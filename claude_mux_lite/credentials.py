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
import signal
import socket
import socketserver
import struct
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping

BROKER_ENV = "CLAUDE_MUX_BROKER"
TOKEN_ENV = "CLAUDE_MUX_API_KEY"
TIMEOUT_HEADER = "X-Broker-Timeout"
UPSTREAM_TIMEOUT_HEADER = "X-Broker-Upstream-Timeout"
TRANSPORT_HEADER = "X-Broker-Transport-Error"
MAX_SOCKET_PATH = 100
MAX_BODY = 64 * 1024 * 1024
# Real credential variables. The bootstrap removes every one of them from the child.
STRIPPED = ("CLAUDE_MUX_API_KEY", "CLAUDE_MUX_HEADERS", "CLAUDE_MUX_BASE_URL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS")
DEFAULT_BASE_URL = "http://127.0.0.1:8788"


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
        request = urllib.request.Request(f"{self.server.upstream}/v1/messages", data=body, headers=headers,
                                         method="POST")
        status, payload, extra = 200, b"", {}
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
    return key, headers, environ.get("CLAUDE_MUX_BASE_URL") or DEFAULT_BASE_URL


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
    folder = Path(tempfile.mkdtemp(prefix="b-", dir=session_dir))
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
        while os.getppid() == parent:
            server.handle_request()
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
