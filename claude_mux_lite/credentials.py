"""A private credential broker. The neutral child never holds a real credential.

`python -m claude_mux_lite --bootstrap SPEC.json` reads the real credentials once from its
inherited environment. It binds a Unix socket in a private directory under the session
directory and forks the broker. It then execs the neutral child with a random bearer token
and the socket path. The child sends its model requests to the broker with that token.
The broker checks the token and the peer, adds the real credentials and forwards the
request to the real endpoint. No spec file, argument list, broker message, tracking row or
log line holds a real credential. The broker dies with its parent and deletes its socket.

Derived from the harness of the MIT-licensed research project.
The full notice is in licenses/RSWARM-MIT.txt.
"""
from __future__ import annotations

import ctypes
import hmac
import http.client
import http.server
import json
import os
import secrets
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Mapping

BROKER_ENV = "CLAUDE_MUX_BROKER"
TOKEN_ENV = "CLAUDE_MUX_API_KEY"
PROVIDER_ENV_VAR = "CLAUDE_MUX_PROVIDER"
TIMEOUT_HEADER = "X-Broker-Timeout"
UPSTREAM_TIMEOUT_HEADER = "X-Broker-Upstream-Timeout"
TRANSPORT_HEADER = "X-Broker-Transport-Error"
MAX_SOCKET_PATH = 100
MAX_BODY = 64 * 1024 * 1024
# Real credential variables. The bootstrap removes every one of them from the child.
STRIPPED = ("CLAUDE_MUX_API_KEY", "CLAUDE_MUX_HEADERS", "CLAUDE_MUX_BASE_URL", PROVIDER_ENV_VAR,
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
            "CLAUDE_CONFIG_DIR", "OPENAI_API_KEY", "OPENAI_BASE_URL", "JBI_OPENAI_BASE_URL",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
DEFAULT_BASE_URL = "http://127.0.0.1:8788"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
AGY_CLI_UPSTREAM = "agy://cli"
_AGY_TURN_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "input": {"type": "object"},
                },
                "required": ["name", "input"],
            },
        },
    },
    "required": ["text", "tool_calls"],
}, separators=(",", ":"))


class BrokerError(RuntimeError):
    """The broker cannot start. The child must not run without it."""


def _redact(text: str, secret_values: list[str]) -> str:
    for value in secret_values:
        if len(value) >= 4:
            text = text.replace(value, "[redacted]")
    return text


def _peer(connection: socket.socket) -> tuple[int, int]:
    """The (pid, uid) that the kernel reports for the other end of a Unix connection."""
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", raw)
    return pid, uid


class _AgyBridge:
    """Serve Anthropic-shaped /v1/messages turns through the local `agy` CLI.

    The CLI runs in an empty private directory with slash commands disabled and
    structured JSON output enforced. It never touches the repository worktree;
    the neutral harness executes every tool call inside its lease fence.
    """

    def __init__(self, secret_values: list[str]):
        self._workdir = Path(tempfile.mkdtemp(prefix="jbi-agy-"))
        os.chmod(self._workdir, 0o700)
        self._conversation_id: str | None = None
        self._sent_count = 0
        self._secrets = list(secret_values)

    def close(self) -> None:
        shutil.rmtree(self._workdir, ignore_errors=True)

    # Linux caps one argv element at MAX_ARG_STRLEN (128 KiB). A prompt travels as
    # one `-p=` argument, so no single piece may approach that bound.
    BLOCK_MAX_CHARS = 24_000

    @staticmethod
    def _flatten_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return json.dumps(content, sort_keys=True)
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text") or ""))
            elif btype == "tool_use":
                parts.append(f"[tool_use {block.get('name')} id={block.get('id')}]: "
                             f"{json.dumps(block.get('input') or {}, sort_keys=True)}")
            elif btype == "tool_result":
                prefix = "ERROR " if block.get("is_error") else ""
                parts.append(f"[tool_result {prefix}for {block.get('tool_use_id')}]: "
                             f"{_AgyBridge._flatten_content(block.get('content'))}")
            else:
                parts.append(json.dumps(block, sort_keys=True))
        return _AgyBridge._cap("\n".join(p for p in parts if p))

    @classmethod
    def _cap(cls, text: str) -> str:
        """Truncate one flattened block past the argv bound, with a visible marker."""
        if len(text) <= cls.BLOCK_MAX_CHARS:
            return text
        return text[:cls.BLOCK_MAX_CHARS] + "\n... [truncated by the jbi agy bridge]"

    @staticmethod
    def _render_full_prompt(payload: dict) -> str:
        system_text = _AgyBridge._flatten_content(payload.get("system") or "")
        tools = payload.get("tools") or []
        tool_lines = []
        for tool in tools:
            if isinstance(tool, dict):
                schema = tool.get("input_schema") or {}
                props = list((schema.get("properties") or {}).keys())
                req = list(schema.get("required") or [])
                tool_lines.append(f"- {tool.get('name')}({', '.join(props)}; required: {', '.join(req)}): "
                                  f"{tool.get('description', '')}")
        parts = [
            "JSON API BRIDGE MODE: Do NOT call any built-in agent tools or shell commands yourself. "
            "Respond ONLY with JSON matching the schema {\"text\": string, \"tool_calls\": [{\"name\": string, \"input\": object}]}. "
            "To execute one or more tools in the fenced workspace, put them in tool_calls and wait for their tool_result. "
            "When the task is complete and verified, return tool_calls=[] and put your final receipt response in text.",
        ]
        if system_text:
            parts.append("SYSTEM:\n" + system_text)
        if tool_lines:
            parts.append("AVAILABLE TOOLS:\n" + "\n".join(tool_lines))
        for msg in payload.get("messages") or []:
            if isinstance(msg, dict):
                role = str(msg.get("role") or "user").upper()
                parts.append(f"{role}:\n{_AgyBridge._flatten_content(msg.get('content'))}")
        return "\n\n".join(parts)

    @staticmethod
    def _render_delta_prompt(messages: list[dict], start_index: int) -> str:
        parts = [
            "Continue in JSON API bridge mode. Do NOT invoke built-in tools; return ONLY JSON "
            "matching {\"text\": string, \"tool_calls\": [{\"name\": string, \"input\": object}]}."
        ]
        for msg in messages[start_index:]:
            if isinstance(msg, dict):
                role = str(msg.get("role") or "user").upper()
                parts.append(f"{role}:\n{_AgyBridge._flatten_content(msg.get('content'))}")
        return "\n\n".join(parts)

    def _invoke(self, model: str, prompt: str, timeout: float, *, conversation_id: str | None) -> subprocess.CompletedProcess:
        binary = shutil.which("agy")
        if not binary:
            raise FileNotFoundError("agy CLI is not installed on PATH")
        argv = [binary, "--model", model, "--output-format", "json",
                "--disable-slash-commands", "--json-schema", _AGY_TURN_SCHEMA]
        if conversation_id:
            argv += ["--conversation", conversation_id]
        argv.append(f"-p={prompt}")
        return subprocess.run(argv, cwd=str(self._workdir), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    # One turn travels as one `-p=` argv element. The kernel caps one element at
    # MAX_ARG_STRLEN (128 KiB); past 100 KiB the exec would die with E2BIG, so the
    # bridge refuses the turn first with a named error the supervisor cools on.
    PROMPT_MAX_CHARS = 100_000

    def handle(self, body: bytes, timeout: float) -> tuple[int, bytes, dict[str, str]]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return 400, f"invalid request JSON: {exc}".encode("utf-8"), {}
        model = str(payload.get("model") or "gemini-4-neon-high")
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        use_conv = bool(self._conversation_id and self._sent_count > 0 and len(messages) > self._sent_count)
        prompt = (self._render_delta_prompt(messages, self._sent_count) if use_conv
                  else self._render_full_prompt(payload))
        if len(prompt) > self.PROMPT_MAX_CHARS:
            return 400, (f"prompt too large for the agy bridge: {len(prompt)} chars "
                         f"(max {self.PROMPT_MAX_CHARS})").encode("utf-8"), {}
        try:
            proc = self._invoke(model, prompt, timeout, conversation_id=self._conversation_id if use_conv else None)
            if proc.returncode != 0 and use_conv:
                self._conversation_id = None
                full = self._render_full_prompt(payload)
                if len(full) > self.PROMPT_MAX_CHARS:
                    return 400, (f"prompt too large for the agy bridge: {len(full)} chars "
                                 f"(max {self.PROMPT_MAX_CHARS})").encode("utf-8"), {}
                proc = self._invoke(model, full, timeout, conversation_id=None)
        except subprocess.TimeoutExpired:
            return 504, b"", {TIMEOUT_HEADER: "1"}
        except FileNotFoundError as exc:
            return 502, b"", {TRANSPORT_HEADER: _redact(str(exc), self._secrets)[:300]}
        except OSError as exc:
            return 502, b"", {TRANSPORT_HEADER: _redact(f"agy launch failed: {exc}", self._secrets)[:300]}
        if proc.returncode != 0:
            raw_err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
            clean = _redact(raw_err, self._secrets)[:500]
            lower = clean.lower()
            if any(tok in lower for tok in ("429", "rate limit", "rate_limit", "quota", "resource_exhausted", "capacity")):
                return 429, clean.encode("utf-8"), {}
            return 502, b"", {TRANSPORT_HEADER: f"agy exited {proc.returncode}: {clean}"[:300]}
        try:
            doc = json.loads(proc.stdout)
        except ValueError:
            clean = _redact((proc.stderr or proc.stdout).strip(), self._secrets)[:300]
            return 502, b"", {TRANSPORT_HEADER: f"agy returned non-JSON output: {clean}"[:300]}
        if isinstance(doc, dict) and isinstance(doc.get("conversation_id"), str) and doc["conversation_id"]:
            self._conversation_id = doc["conversation_id"]
            self._sent_count = len(messages)
        structured = doc.get("structured_output") if isinstance(doc, dict) else None
        if not isinstance(structured, dict):
            raw_resp = doc.get("response") if isinstance(doc, dict) else ""
            if isinstance(raw_resp, str) and raw_resp.strip():
                try:
                    parsed = json.loads(raw_resp)
                    structured = parsed if isinstance(parsed, dict) else {"text": raw_resp, "tool_calls": []}
                except ValueError:
                    structured = {"text": raw_resp, "tool_calls": []}
            else:
                structured = {"text": "", "tool_calls": []}
        text = str(structured.get("text") or "")
        raw_calls = structured.get("tool_calls") if isinstance(structured.get("tool_calls"), list) else []
        content: list[dict[str, Any]] = []
        if text.strip():
            content.append({"type": "text", "text": text})
        has_tools = False
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            name = call.get("name")
            inp = call.get("input")
            if isinstance(name, str) and name.strip() and isinstance(inp, dict):
                has_tools = True
                content.append({
                    "type": "tool_use",
                    "id": f"toolu_{uuid.uuid4().hex[:12]}",
                    "name": name.strip(),
                    "input": inp,
                })
        if not content:
            content.append({"type": "text", "text": text})
        raw_usage = doc.get("usage") if isinstance(doc, dict) and isinstance(doc.get("usage"), dict) else {}
        usage: dict[str, int] = {}
        for dst, src in (("input_tokens", "input_tokens"),
                         ("output_tokens", "output_tokens"),
                         ("cache_read_input_tokens", "cache_read_tokens")):
            val = raw_usage.get(src)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                usage[dst] = val
        answer: dict[str, Any] = {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": "tool_use" if has_tools else "end_turn",
        }
        if usage:
            answer["usage"] = usage
        return 200, json.dumps(answer, separators=(",", ":")).encode("utf-8"), {}


class _Server(socketserver.UnixStreamServer):
    """One request at a time. One client process only."""

    allow_reuse_address = False
    request_queue_size = 4

    def __init__(self, sock: socket.socket, handler, *, token: str, upstream: str, key: str,
                 headers: dict[str, str]):
        # The socket is bound and listening already, so no client can connect before the fork.
        socketserver.BaseServer.__init__(self, sock.getsockname(), handler)
        self.socket = sock
        self.token, self.upstream, self.key, self.extra = token, upstream.rstrip("/"), key, headers
        self.client_pid: int | None = None
        self.secret_values = [key, *headers.values()]
        self.agy_bridge = _AgyBridge(self.secret_values) if self.upstream == AGY_CLI_UPSTREAM else None

    def close_bridge(self) -> None:
        if self.agy_bridge is not None:
            self.agy_bridge.close()

    def server_bind(self) -> None:          # bound by the caller
        pass

    def server_activate(self) -> None:      # listening by the caller
        pass

    def get_request(self):
        connection, address = self.socket.accept()
        return connection, address


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server: _Server

    def log_message(self, *_: object) -> None:      # never log
        pass

    def _refuse(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        pid, uid = _peer(self.request)
        sent = self.headers.get("Authorization", "")
        if uid != os.getuid():
            return self._refuse(403)
        if not hmac.compare_digest(sent.encode(), f"Bearer {self.server.token}".encode()):
            return self._refuse(401)
        if self.server.client_pid is None:
            self.server.client_pid = pid
        elif pid != self.server.client_pid:
            return self._refuse(403)
        if self.path != "/v1/messages":
            return self._refuse(404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._refuse(400)
        if not 0 <= length <= MAX_BODY:
            return self._refuse(413)
        body = self.rfile.read(length)
        headers = {"Content-Type": "application/json",
                   "anthropic-version": self.headers.get("anthropic-version", "2023-06-01")}
        headers.update(self.server.extra)
        if self.server.key:
            headers["Authorization"] = f"Bearer {self.server.key}"
        try:
            timeout = min(float(self.headers.get(UPSTREAM_TIMEOUT_HEADER, "1200")), 3600.0)
        except ValueError:
            timeout = 1200.0
        status, payload, extra = 200, b"", {}
        if self.server.agy_bridge is not None:
            status, payload, extra = self.server.agy_bridge.handle(body, timeout)
        else:
            request = urllib.request.Request(f"{self.server.upstream}/v1/messages", data=body, headers=headers,
                                             method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    status, payload = response.status, response.read()
            except urllib.error.HTTPError as error:
                status = error.code
                payload = _redact(error.read().decode("utf-8", "replace")[:500], self.server.secret_values).encode()
                retry = error.headers.get("Retry-After") if error.headers else None
                if retry:
                    extra["Retry-After"] = retry
            except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as error:
                if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
                    status, extra = 504, {TIMEOUT_HEADER: "1"}
                else:
                    status = 502
                    extra = {TRANSPORT_HEADER: _redact(f"transport failure: {error}", self.server.secret_values)[:300]}
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)


def _parse_custom_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        name, value = name.strip(), value.strip()
        if name and value:
            headers[name] = value
    return headers


def _claude_oauth_token(environ: Mapping[str, str]) -> str:
    config_dir = environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else (Path(environ.get("HOME") or str(Path.home())) / ".claude")
    cred_path = base / ".credentials.json"
    try:
        st = cred_path.stat()
        if not cred_path.is_file() or st.st_uid != os.getuid():
            return ""
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    oauth = data.get("claudeAiOauth")
    token = oauth.get("accessToken") if isinstance(oauth, dict) else ""
    return token.strip() if isinstance(token, str) else ""


def take_from_environment(environ: Mapping[str, str]) -> tuple[str, dict[str, str], str]:
    """The real key, the extra headers and the real base URL. Read once from the environment."""
    key = environ.get("CLAUDE_MUX_API_KEY", "")
    raw = environ.get("CLAUDE_MUX_HEADERS")
    headers: dict[str, str] = {}
    if raw:
        try:
            headers = json.loads(raw)
        except ValueError:
            raise BrokerError("CLAUDE_MUX_HEADERS is not valid JSON") from None
        if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                    for k, v in headers.items()):
            raise BrokerError("CLAUDE_MUX_HEADERS must be a JSON object of strings")
    explicit_base = environ.get("CLAUDE_MUX_BASE_URL")
    if explicit_base:
        return key, headers, explicit_base
    provider = (environ.get(PROVIDER_ENV_VAR) or "").strip().lower()
    if provider == "agy":
        return key, headers, AGY_CLI_UPSTREAM
    if provider == "claude":
        custom = _parse_custom_headers(environ.get("ANTHROPIC_CUSTOM_HEADERS") or "")
        merged_headers = {**custom, **headers}
        if not key:
            key = (environ.get("ANTHROPIC_API_KEY")
                   or environ.get("ANTHROPIC_AUTH_TOKEN")
                   or _claude_oauth_token(environ))
        if key.startswith("sk-ant-oat"):
            merged_headers.setdefault("anthropic-beta", "oauth-2025-04-20")
        upstream = environ.get("ANTHROPIC_BASE_URL") or ANTHROPIC_BASE_URL
        return key, merged_headers, upstream
    if provider == "openai-compatible":
        upstream = environ.get("JBI_OPENAI_BASE_URL") or environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
        if not key:
            key = environ.get("OPENAI_API_KEY") or ""
        return key, headers, upstream
    if provider == "codex":
        raise BrokerError("provider codex has no neutral dispatch bridge; the codex CLI "
                          "is a completion lane, not an agent transport")
    return key, headers, DEFAULT_BASE_URL


def _pdeathsig() -> None:
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGTERM), 0, 0, 0)   # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def start_broker(session_dir: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """Bind the broker socket, fork the broker and return the environment for the neutral child.

    The returned variables are only the random token and the socket path."""
    key, headers, upstream = take_from_environment(environ)
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    socket_parent = None
    for candidate in (session_dir.parent, session_dir.parent.parent, Path(tempfile.gettempdir())):
        while True:
            try:
                stat = candidate.stat()
            except OSError:
                break
            if stat.st_uid != os.getuid() or stat.st_mode & 0o002:
                break
            try:
                if len(str(candidate / ".jbi-b-12345678/s.sock")) <= MAX_SOCKET_PATH:
                    socket_parent = candidate
                    break
            except OSError:
                break
            if candidate == Path(candidate.anchor):
                break
            candidate = candidate.parent
        if socket_parent is not None:
            break
    if socket_parent is None:
        raise BrokerError("no bounded private broker socket path is available")
    folder = Path(tempfile.mkdtemp(prefix=".jbi-b-", dir=socket_parent))
    os.chmod(folder, 0o700)
    path = folder / "s.sock"
    if len(str(path)) > MAX_SOCKET_PATH:
        os.rmdir(folder)
        raise BrokerError("the session directory path is too long for a Unix socket")
    token = secrets.token_urlsafe(48)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    sock.listen(4)
    parent = os.getpid()
    pid = os.fork()
    if pid:
        sock.close()
        return {TOKEN_ENV: token, BROKER_ENV: str(path)}
    # ---- the broker process ----
    code = 0
    try:
        for name in STRIPPED:
            os.environ.pop(name, None)
        _pdeathsig()
        null = os.open(os.devnull, os.O_RDWR)
        for descriptor in (0, 1, 2):            # the broker must not hold the pipes of the child
            os.dup2(null, descriptor)
        if os.getppid() != parent:
            raise SystemExit(0)

        def stop(*_: object) -> None:
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
        server = _Server(sock, _Handler, token=token, upstream=upstream, key=key, headers=headers)
        server.timeout = 0.5
        try:
            while os.getppid() == parent:
                server.handle_request()
        finally:
            server.close_bridge()
    except SystemExit:
        pass
    except BaseException:
        code = 1
    finally:
        try:
            os.unlink(path)
            os.rmdir(folder)
        except OSError:
            pass
        os._exit(code)
    return {}


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("broker", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def post(path: str, token: str, payload: dict, headers: dict[str, str], timeout: float) -> tuple[int, dict, bytes]:
    """Send one request through the broker. Returns (status, response headers, body)."""
    connection = _UnixConnection(path, timeout)
    try:
        send = {**headers, "Authorization": f"Bearer {token}", UPSTREAM_TIMEOUT_HEADER: str(timeout)}
        connection.request("POST", "/v1/messages", body=json.dumps(payload).encode("utf-8"), headers=send)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def exec_child(spec: str, environ: Mapping[str, str], extra: Mapping[str, str]) -> None:
    """Replace this process with the neutral child. Only `extra` carries credential material."""
    env = {k: v for k, v in environ.items() if k not in STRIPPED}
    env.update(extra)
    os.execve(sys.executable, [sys.executable, "-m", "claude_mux_lite", spec], env)
