"""claude-mux-lite: a minimal, provider-neutral coding harness. Standard library only.

This module derives selectively from the harness of the MIT-licensed rswarm project
(Copyright (c) 2026 Madhav Shroff). The full license text is in licenses/RSWARM-MIT.txt.
It imports no rswarm module and copies no research method or research tool.

The harness speaks the raw HTTP protocol: POST {CLAUDE_MUX_BASE_URL}/v1/messages with API
version 2023-06-01 and bearer auth from CLAUDE_MUX_API_KEY. It exposes six fenced tools.
Every write needs a lease through the injected tracking object.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

API_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "http://127.0.0.1:8788"
EPHEMERAL = {"type": "ephemeral"}
MAX_EXPLICIT_MARKS = 4
READ_MAX_LINES = 400
READ_MAX_CHARS = 24000
BASH_OUT_MAX = 16000
RECEIPT_MAX = 8000
RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504, 529})
RECEIPT_LINE = re.compile(r"^BRIEF\s+\S+\s*\|\s*(?:[0-9a-f]{7,64}|uncommitted)\s*\|\s*(?:PASS|FAIL)\s*$")


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None


@dataclass
class HarnessResult:
    final_text: str = ""
    exit_code: int = 0
    timed_out: bool = False
    turns: int = 0
    usage: Usage = field(default_factory=Usage)


# ---- errors and redaction ----------------------------------------------------------------


class HttpError(Exception):
    """The endpoint answered with an error status."""

    def __init__(self, status: int, body: str = "", retry_after: float | None = None):
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body, self.retry_after = status, body, retry_after


class ProtocolError(Exception):
    """The endpoint answer was not valid JSON or not an object."""


class ToolError(Exception):
    """A tool refused its input. The message goes back to the model."""


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every secret of 4 characters or more with a marker."""
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    return text


def _secrets(env: dict[str, str]) -> list[str]:
    out = [env.get("CLAUDE_MUX_API_KEY", "")]
    try:
        extra = json.loads(env.get("CLAUDE_MUX_HEADERS") or "{}")
    except ValueError:
        extra = {}
    if isinstance(extra, dict):
        out += [v for v in extra.values() if isinstance(v, str)]
    return [s for s in out if s]


class HttpTransport:
    """Raw HTTP transport. It reads the endpoint from the environment and never logs a key."""

    def __init__(self, timeout: float = 1200.0, env: dict[str, str] | None = None):
        env = dict(os.environ if env is None else env)
        self.base = (env.get("CLAUDE_MUX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._secrets = _secrets(env)
        self.broker = env.get("CLAUDE_MUX_BROKER") or None
        self.headers = {"Content-Type": "application/json", "anthropic-version": API_VERSION}
        raw = env.get("CLAUDE_MUX_HEADERS")
        if raw:
            try:
                extra = json.loads(raw)
            except ValueError:
                raise ValueError("CLAUDE_MUX_HEADERS is not valid JSON") from None
            if not isinstance(extra, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                      for k, v in extra.items()):
                raise ValueError("CLAUDE_MUX_HEADERS must be a JSON object of strings")
            self.headers.update(extra)
        if env.get("CLAUDE_MUX_API_KEY"):
            self.headers["Authorization"] = f"Bearer {env['CLAUDE_MUX_API_KEY']}"

    def _via_broker(self, payload: dict) -> str:
        """Send through the private credential broker. The child holds only a random token."""
        from . import credentials
        try:
            status, headers, raw = credentials.post(self.broker, self.headers["Authorization"].split(" ", 1)[1],
                                                    payload, {"Content-Type": "application/json",
                                                              "anthropic-version": API_VERSION}, self.timeout)
        except (TimeoutError, OSError, ValueError) as exc:
            if isinstance(exc, TimeoutError):
                raise TimeoutError("request timed out") from None
            raise ProtocolError(redact(f"transport failure: {exc}", self._secrets)) from None
        text = raw.decode("utf-8", errors="replace")
        if headers.get(credentials.TIMEOUT_HEADER):
            raise TimeoutError("request timed out")
        if headers.get(credentials.TRANSPORT_HEADER):
            raise ProtocolError(redact(headers[credentials.TRANSPORT_HEADER], self._secrets))
        if status >= 400:
            retry = None
            try:
                retry = float(headers.get("Retry-After")) if headers.get("Retry-After") else None
            except (TypeError, ValueError):
                pass
            raise HttpError(status, redact(text[:500], self._secrets), retry)
        return text

    def __call__(self, payload: dict) -> dict:
        if self.broker:
            body = self._via_broker(payload)
            try:
                doc = json.loads(body)
            except ValueError:
                raise ProtocolError("endpoint returned invalid JSON") from None
            if not isinstance(doc, dict):
                raise ProtocolError("endpoint returned a non-object answer")
            return doc
        req = urllib.request.Request(f"{self.base}/v1/messages", data=json.dumps(payload).encode("utf-8"),
                                     headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            text = redact(exc.read().decode("utf-8", errors="replace")[:500], self._secrets)
            retry = None
            try:
                retry = float(exc.headers.get("Retry-After")) if exc.headers else None
            except (TypeError, ValueError):
                pass
            raise HttpError(exc.code, text, retry) from None
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
            if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
                raise TimeoutError("request timed out") from None
            raise ProtocolError(redact(f"transport failure: {exc}", self._secrets)) from None
        try:
            doc = json.loads(body)
        except ValueError:
            raise ProtocolError("endpoint returned invalid JSON") from None
        if not isinstance(doc, dict):
            raise ProtocolError("endpoint returned a non-object answer")
        return doc


# ---- usage -------------------------------------------------------------------------------


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_usage(raw: Any) -> Usage:
    """Map one response usage object. An absent or invalid field stays None, never zero."""
    if not isinstance(raw, dict):
        return Usage()
    return Usage(_count(raw.get("input_tokens")), _count(raw.get("output_tokens")),
                 _count(raw.get("cache_read_input_tokens")), _count(raw.get("cache_creation_input_tokens")))


def _add(a: int | None, b: int | None) -> int | None:
    return None if a is None and b is None else (a or 0) + (b or 0)


def add_usage(a: Usage, b: Usage) -> Usage:
    return Usage(_add(a.input_tokens, b.input_tokens), _add(a.output_tokens, b.output_tokens),
                 _add(a.cache_read, b.cache_read), _add(a.cache_write, b.cache_write))


# ---- tools and request building ----------------------------------------------------------


def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": required}}


_S = {"type": "string"}
_I = {"type": "integer"}
TOOLS: list[dict] = [
    _tool("bash", "Run one bash command in the workspace. Writes outside leased paths are reverted.",
          {"command": _S, "timeout_sec": _I}, ["command"]),
    _tool("read_file", "Read numbered lines of a workspace file. Limit 400 lines.",
          {"path": _S, "start_line": _I, "end_line": _I}, ["path"]),
    _tool("write_file", "Create or overwrite a leased workspace file.", {"path": _S, "content": _S},
          ["path", "content"]),
    _tool("edit_file", "Replace one exact unique text in a leased workspace file.",
          {"path": _S, "old_text": _S, "new_text": _S}, ["path", "old_text", "new_text"]),
    _tool("list_dir", "List one workspace directory.", {"path": _S}, []),
    _tool("grep_files", "Search workspace files with a regex.", {"pattern": _S, "path": _S}, ["pattern"]),
]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)

SYSTEM_RULES = (
    "You work in one workspace through exactly six tools, in this order: bash, read_file, write_file, "
    "edit_file, list_dir, grep_files.\n"
    "A lease limits every write. A write outside the leased paths fails or reverts.\n"
    "Never run git stash. Never write outside the workspace.\n"
    "Follow the brief exactly. When done, make the first line of your final answer: "
    "BRIEF <task> | <sha-or-uncommitted> | PASS or FAIL."
)
STABLE_BRIEF_CONTEXT = "The brief follows in the next block. Treat it as the only task."


def build_system(repo_context: str = "", frozen_contracts: str = "") -> list[dict]:
    """One system block: invariant rules, then repo context, then frozen contracts. No volatile text."""
    parts = [SYSTEM_RULES] + [p for p in (repo_context, frozen_contracts) if p and p.strip()]
    return [{"type": "text", "text": "\n\n".join(parts), "cache_control": dict(EPHEMERAL)}]


def build_user_content(brief: str, first: bool) -> list[dict]:
    """The first message holds the stable block, then the task block. Later messages hold the task."""
    task = "Brief:\n" + (brief if brief.strip() else "(empty)")
    if first:
        return [{"type": "text", "text": STABLE_BRIEF_CONTEXT, "cache_control": dict(EPHEMERAL)},
                {"type": "text", "text": task}]
    return [{"type": "text", "text": task}]


def strip_marks(messages: list[dict]) -> list[dict]:
    """Deep copy of `messages` without any cache_control key."""
    out = copy.deepcopy(messages)
    for msg in out:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    return out


def _markable(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    if block.get("type") == "text":
        return bool(str(block.get("text") or "").strip())
    if block.get("type") == "tool_result":
        return bool(block.get("content"))
    return False


def apply_cache_marks(messages: list[dict]) -> list[dict]:
    """Deep copy with two marks: the first block of the first message, the last block of the history.

    A mark never lands on tool_use, thinking, or empty content."""
    out = strip_marks(messages)
    if not out:
        return out
    first = out[0].get("content")
    if isinstance(first, list) and first and _markable(first[0]):
        first[0]["cache_control"] = dict(EPHEMERAL)
    last = out[-1].get("content")
    if isinstance(last, list):
        for block in reversed(last):
            if _markable(block):
                block["cache_control"] = dict(EPHEMERAL)
                break
    return out


def build_payload(*, model: str, max_tokens: int, effort: str, system: list[dict],
                  messages: list[dict]) -> dict:
    return {"model": model, "max_tokens": max_tokens, "output_config": {"effort": effort},
            "tools": TOOLS, "system": system, "messages": apply_cache_marks(messages),
            "cache_control": dict(EPHEMERAL)}


def count_marks(payload: dict) -> int:
    """Count explicit cache_control marks in system and messages, plus the top-level one."""
    total = 1 if "cache_control" in payload else 0
    for block in payload.get("system", []):
        total += 1 if isinstance(block, dict) and "cache_control" in block else 0
    for msg in payload.get("messages", []):
        for block in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
            total += 1 if isinstance(block, dict) and "cache_control" in block else 0
    return total


# ---- paths and the bash fence ------------------------------------------------------------


def path_matches(path: str, owns: Iterable[str]) -> bool:
    """True when `path` equals an owns entry, sits under it, or matches its glob."""
    for pattern in owns:
        if path == pattern or path.startswith(pattern + "/"):
            return True
        if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def inside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


WRITERS = frozenset({"sed", "cp", "mv", "mkdir", "touch", "rm", "rmdir", "ln", "install", "chmod", "chown",
                     "chgrp", "dd", "truncate", "rsync", "find", "tee", "patch", "unlink", "shred", "cd",
                     "pushd", "git", "tar", "unzip", "curl", "wget"})
WRAPPERS = frozenset({"env", "nohup", "nice", "time", "timeout", "exec", "command", "builtin", "xargs"})
INTERPRETERS = frozenset({"node", "perl", "ruby", "bash", "sh", "zsh", "php", "lua", "awk"})
SAFE_DEV = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "|&"})
REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", ">&"})
ABS_IN_CODE = re.compile(r"(?<![\w.:/])/(?!dev/(?:null|stdout|stderr)\b)[A-Za-z0-9_.~-]+")
DOTDOT_IN_CODE = re.compile(r"(?:^|[\s'\"=(,\[])\.\.(?:/|$|[\s'\")\],])")
STASH = re.compile(r"\bgit\b[^\n;&|]*\bstash\b")


SAFE_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "diff.external=")
DISCOVERY_GIT = ("--no-optional-locks", "-c", "protocol.file.allow=never")


def host_git_argv(*args: str, work_tree: Path | str | None = None, git_dir: Path | str | None = None,
                  discovery: bool = True) -> list[str]:
    """Build a Git argument list for fixed host code.

    The caller must authorize writes and pin storage before an agent can edit the worktree."""
    argv = ["git", *SAFE_GIT,
            "--attr-source=4b825dc642cb6eb9a060e54bf8d69288fbee4904",
            "-c", "core.attributesFile=/dev/null", "-c", "core.untrackedCache=false",
            "-c", "commit.gpgSign=false", "-c", "gpg.program=true", "-c", "core.pager=cat"]
    if discovery:
        argv += list(DISCOVERY_GIT)
    if git_dir is not None:
        argv += [f"--git-dir={git_dir}"]
        if work_tree is not None:
            argv += [f"--work-tree={work_tree}"]
    elif work_tree is not None:
        argv += ["-C", str(work_tree)]
    return argv + list(args)


def fence_env() -> dict[str, str]:
    """The environment of a fenced shell. It holds no credential."""
    env = {k: os.environ[k] for k in CHILD_ENV if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def host_git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in CHILD_ENV or k.startswith("LC_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    return env


class GuardError(Exception):
    """The fence cannot parse or cannot safely repair a state."""


def _plain_newlines(cmd: str) -> str:
    """Replace unquoted newlines with ';'. Quotes and backslashes keep their meaning."""
    out, quote, i = [], "", 0
    while i < len(cmd):
        ch = cmd[i]
        if ch == "\\" and quote != "'" and i + 1 < len(cmd):
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            ch = " ; "
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_heredocs(cmd: str) -> str:
    lines, out, end = cmd.split("\n"), [], None
    for line in lines:
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", line)
        if m:
            end = m.group(1)
    return "\n".join(out)


def parse_status(raw: bytes) -> list[tuple[str, str, str | None]]:
    """Exact parser for `git status --porcelain=v1 -z`. Returns (xy, path, original path or None)."""
    parts = raw.split(b"\0")
    out: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            raise GuardError("unparseable git status entry")
        xy, path = entry[:2].decode("ascii", "replace"), entry[3:].decode("utf-8", "surrogateescape")
        orig = None
        if "R" in xy or "C" in xy:
            if i >= len(parts) or not parts[i]:
                raise GuardError("rename entry lacks its origin path")
            orig = parts[i].decode("utf-8", "surrogateescape")
            i += 1
        out.append((xy, path, orig))
    return out


class BashGuard:
    """Conservative fence for shell commands: a parsed pre-check and a git-based post-check."""

    def __init__(self, workdir: Path, owns: Iterable[str], authorize: Callable[[str], bool] | None = None,
                 allow_commits: bool = False):
        self.root = Path(os.path.realpath(workdir))
        self.owns = tuple(owns)
        self._ignored_backup: dict[str, tuple[str, os.stat_result | None, bytes | None, str | None]] = {}
        self.authorize = authorize
        self.allow_commits = allow_commits
        from .sandbox import discover_git
        self.git_dir, self.common_dir = discover_git(self.root)
        self.head = self._rev()
        self.branch = self._git("symbolic-ref", "-q", "HEAD")[1].decode().strip()
        self.baseline: dict[str, tuple[str, str]] = {}
        self.baseline = self.snapshot()

    # -- git helpers --
    def _git(self, *args: str) -> tuple[int, bytes]:
        """A read-only Git call on the host. It uses safe config and the pinned Git folder."""
        proc = subprocess.run(host_git_argv(*args, work_tree=self.root, git_dir=self.git_dir),
                              capture_output=True, stdin=subprocess.DEVNULL, timeout=120, env=host_git_env())
        return proc.returncode, proc.stdout

    def _git_fenced(self, *args: str) -> int:
        """Only fixed reconciliation operations use host Git with pinned storage."""
        if not args or args[0] not in ("checkout", "reset", "rm", "read-tree", "update-ref"):
            return 1
        proc = subprocess.run(host_git_argv(*args, work_tree=self.root, git_dir=self.git_dir),
                              capture_output=True, stdin=subprocess.DEVNULL, timeout=120,
                              env=host_git_env())
        return proc.returncode

    def _rev(self) -> str:
        rc, out = self._git("rev-parse", "--verify", "-q", "HEAD")
        return out.decode().strip() if rc == 0 else ""

    def _status(self) -> list[tuple[str, str, str | None]]:
        # Ignored dependency trees remain compact at their first matched directory.
        rc, out = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
        if rc != 0:
            raise GuardError("git status failed")
        ignored_rc, ignored_raw = self._git(
            "ls-files", "-o", "-i", "--exclude-standard", "--directory", "-z")
        if ignored_rc != 0:
            raise GuardError("Git ignored-file discovery failed")
        ignored = [("!!", item.decode("utf-8", "surrogateescape"), None)
                   for item in ignored_raw.split(b"\0") if item]
        return self._ignored_entries(parse_status(out) + ignored)

    def _ignored_entries(self, entries: list[tuple[str, str, str | None]]) -> list[tuple[str, str, str | None]]:
        """Enumerate known ignored trees. Other ignored files stay explicit entries."""
        current = {rel: (xy, rel, orig) for xy, rel, orig in entries}
        for xy, rel, original in entries:
            if xy != "!!" or not rel.endswith("/") or not any(item.startswith(rel) for item in self.baseline):
                continue
            rc, raw = self._git("ls-files", "-o", "-i", "--exclude-standard", "-z", "--", rel)
            if rc != 0 or raw.count(b"\0") > 10000:
                continue
            for item in raw.split(b"\0"):
                child = item.decode("utf-8", "surrogateescape") if item else ""
                if child != rel:
                    current[child] = ("!!", child, None)
        return list(current.values())

    def _signature(self, path: str) -> str:
        full = self.root / path
        try:
            st = os.lstat(full)
        except OSError:
            return "gone"
        if os.path.islink(full):
            return "link:" + os.readlink(full)
        if st.st_size <= 4_000_000:
            try:
                return "sha:" + hashlib.sha1(full.read_bytes()).hexdigest()
            except OSError:
                pass
        return f"stat:{st.st_size}:{st.st_mtime_ns}"

    def snapshot(self) -> dict[str, tuple[str, str]]:
        """Map each dirty path to (status, content signature)."""
        snap: dict[str, tuple[str, str]] = {}
        entries = self._status()
        for xy, path, orig in entries:
            for p in (path, orig):
                if p:
                    snap[p] = (xy, self._signature(p))
        directories = [path for xy, path, _ in entries if xy == "!!" and path.endswith("/")]
        for directory in directories:
            # Enumerate real ignored leaves, not the aggregate directory status.
            rc, raw = self._git("ls-files", "-o", "-i", "--exclude-standard", "-z", "--", directory)
            if rc != 0 or raw.count(b"\0") > 10000:
                continue
            for item in raw.split(b"\0"):
                if item:
                    child = item.decode("utf-8", "surrogateescape")
                    snap[child] = ("!!", self._signature(child))
                    if child not in self._ignored_backup:
                        file = self._safe(child)
                        try:
                            if file and not file.is_symlink() and file.is_file() and file.stat().st_size <= 4_000_000:
                                self._ignored_backup[child] = ("file", file.stat(), file.read_bytes(), None)
                            elif file and file.is_symlink():
                                self._ignored_backup[child] = ("link", None, None, os.readlink(file))
                        except OSError:
                            continue
        return snap

    def owned(self, path: str) -> bool:
        clean = path[2:] if path.startswith("./") else path
        if not path_matches(clean, self.owns):
            return False
        if self.authorize is None:
            return True
        try:
            return bool(self.authorize(clean))
        except Exception:
            return False

    # -- pre-check --
    def _bad_path(self, arg: str) -> str | None:
        unresolved = arg.replace("${TMPDIR}", "").replace("$TMPDIR", "")
        if "$" in unresolved or "`" in arg or arg.startswith("~"):
            return f"unresolved expansion in {arg!r}"
        if arg in SAFE_DEV or arg == "$TMPDIR" or arg.startswith(("$TMPDIR/", "${TMPDIR}/")):
            return None
        if any(part == ".." for part in arg.split("/")):
            return f"parent traversal in {arg!r}"
        target = Path(os.path.realpath(arg if arg.startswith("/") else self.root / arg))
        if not inside(self.root, target):
            return f"{arg!r} is outside the workspace"
        return None

    def _segment(self, words: list[str]) -> str | None:
        if words and any(ch in words[0] for ch in "$`~"):
            return "expanded command names and paths are refused"
        i = 0
        while i < len(words) and (("=" in words[i] and not words[i].startswith("-")) or words[i] in WRAPPERS
                                  or words[i].startswith("-") or words[i].isdigit()):
            i += 1
        if i >= len(words):
            return None
        cmd, args = os.path.basename(words[i]), words[i + 1:]
        if cmd in ("sudo", "su", "doas"):
            return "privilege escalation is refused"
        if cmd in ("mkdir", "rmdir"):
            for arg in (a for a in args if not a.startswith("-")):
                bad = self._bad_path(arg)
                rel = os.path.relpath(os.path.realpath(self.root / arg), self.root) if not bad else ""
                if bad or not self.owned(rel):
                    return f"{cmd} target refused: {bad or arg + ' is not leased'}"
        if cmd == "tee":
            for arg in (a for a in args if not a.startswith("-")):
                bad = self._bad_path(arg)
                if bad:
                    return f"tee target refused: {bad}"
        if cmd == "git":
            if any(any(ch in a for ch in "$`~") for a in args):
                return "expanded Git options and verbs are refused"
            if any(a in ("-c", "--config-env") or a.startswith(("-c", "--config-env=")) for a in args):
                i = 0
                configs: list[str] = []
                while i < len(args) and (args[i] == "-c" or args[i].startswith("-c")):
                    if args[i] == "-c" and i + 1 < len(args):
                        configs.append(args[i + 1])
                        i += 2
                    elif args[i].startswith("-c") and len(args[i]) > 2:
                        configs.append(args[i][2:])
                        i += 1
                    else:
                        return "Git configuration is reserved for the runtime"
                if any(not re.fullmatch(r"(?:user\.name|user\.email)=[^\x00\r\n]{1,256}", item)
                       for item in configs):
                    return "Git configuration is reserved for the runtime"
            gargs = [a for a in args if not a.startswith("-")]
            verb = next((i for i, a in enumerate(args) if not a.startswith("-")
                         and (i == 0 or args[i - 1] not in ("-C", "--git-dir", "--work-tree", "--exec-path", "-c"))), None)
            if any(a in ("--exec-path", "--work-tree", "--git-dir", "-C") for a in args):
                return "Git directory and execution options are reserved for the runtime"
            subcommand = args[verb] if verb is not None else ""
            read_commands = {"add", "commit", "diff", "log", "show", "status", "ls-files", "rev-list",
                             "rev-parse", "cat-file", "checkout", "grep", "rm", "blame",
                             "describe", "shortlog", "show-ref", "for-each-ref", "branch", "reset", "clean"}
            if subcommand == "clean":
                if "--" not in args or any(arg in ("-x", "-X", "-a", "-A") for arg in args):
                    return "a shell may clean only explicit leased paths"
                for path in args[args.index("--") + 1:]:
                    if not self.owned(path):
                        return f"clean target is outside the task lease: {path}"
            if subcommand not in read_commands:
                return f"git {subcommand or '<unspecified>'} is confined to the private task index"
            if subcommand == "branch" and (not gargs or len(gargs) > 1 or any(a.startswith("-") for a in args)):
                return "git branch mutations are confined to the runtime"
            if subcommand in ("commit", "rm") and not self.allow_commits:
                return "this task does not allow shell Git mutations"
            if subcommand == "reset" and "--" not in args:
                return "a shell reset may name only paths"
            if subcommand == "checkout" and (any(a in ("-b", "-B", "-c", "-C", "--orphan", "--detach", "-f", "--force")
                                                for a in args) or "--" not in args):
                return "a shell checkout may restore only files from the pinned HEAD"
            if subcommand in ("checkout", "switch") and any(a in ("-b", "-B", "-c", "-C", "--orphan", "--detach") for a in args):
                return "a shell cannot create or detach a leased branch"
            if any(a in ("--global", "--system", "--file", "-f") for a in args if subcommand == "config"):
                return "only local leased config is permitted"
            if subcommand == "reset" and any(a not in ("HEAD", "--") for a in args[1:]):
                return "a shell reset may restore only HEAD paths"
            if subcommand == "checkout" and any(
                    (a.startswith("-") or re.fullmatch(r"[0-9a-fA-F]{4,64}", a))
                    and a not in ("HEAD", "--") for a in args[1:]):
                return "a shell checkout may restore only current HEAD paths"
            if subcommand in ("add", "rm", "checkout", "reset") and "--" in args:
                for path in args[args.index("--") + 1:]:
                    if path.startswith("("):
                        return "Git pathspec expressions are refused"
                    if not self.owned(path):
                        # Metadata and parent symlink changes cannot become leased source.
                        return f"staged path is outside the task lease: {path}"
            for j, arg in enumerate(args):
                value = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else None
                if arg in ("-C", "--git-dir", "--work-tree") and j + 1 < len(args):
                    value = args[j + 1]
                bad = self._bad_path(value) if value else None
                if bad:
                    return f"git target refused: {bad}"
        elif cmd == "find":
            if any(a in ("-delete", "-exec", "-execdir", "-ok", "-fprint", "-fprintf", "-fls") for a in args):
                for arg in (a for a in args if not a.startswith("-") and a not in ("(", ")", "!", "{}")):
                    if "/" in arg or arg == "..":
                        bad = self._bad_path(arg)
                        if bad:
                            return f"find target refused: {bad}"
        elif cmd == "sed" or cmd in WRITERS:
            skip_script = cmd == "sed" and not any(a in ("-e", "-f") or a.startswith("--expression")
                                                   for a in args)
            for arg in args:
                if arg.startswith("-"):
                    value = arg.split("=", 1)[1] if "=" in arg else ""
                    bad = self._bad_path(value) if value.startswith(("/", "..", "~")) else None
                elif skip_script:
                    skip_script = False
                    continue
                else:
                    bad = self._bad_path(arg)
                if bad:
                    return f"{cmd} argument refused: {bad}"
        if cmd.startswith("python") or cmd in INTERPRETERS:
            for arg in args:
                if arg.startswith("-") and len(arg) < 4:
                    continue
                if ABS_IN_CODE.search(arg):
                    for m in ABS_IN_CODE.finditer(arg):
                        if not inside(self.root, Path(os.path.realpath(m.group(0)))):
                            return f"{cmd} code names a path outside the workspace: {m.group(0)}"
                if DOTDOT_IN_CODE.search(arg):
                    return f"{cmd} code uses parent traversal"
            if cmd in ("bash", "sh", "zsh") and "-c" in args and args.index("-c") + 1 < len(args):
                return self.check(args[args.index("-c") + 1])
        return None

    def check(self, command: str) -> str | None:
        """Return None when the command passes the fence, else the refusal reason."""
        if STASH.search(command):
            return "git stash is refused"
        try:
            text = _plain_newlines(_strip_heredocs(command))
            lex = shlex.shlex(text, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            tokens = list(lex)
        except ValueError:
            return "command is unparseable"
        segment: list[str] = []
        segments: list[list[str]] = []
        for i, tok in enumerate(tokens):
            if tok in REDIRECTS and i + 1 < len(tokens):
                target = tokens[i + 1]
                if not (tok == ">&" and (target.isdigit() or target == "-")):
                    bad = self._bad_path(target)
                    if bad:
                        return f"redirect refused: {bad}"
            if tok in SEPARATORS:
                segments.append(segment)
                segment = []
            elif not set(tok) <= set("<>&|;()"):
                segment.append(tok)
        segments.append(segment)
        for words in segments:
            reason = self._segment(words)
            if reason:
                return reason
        return None

    # -- post-check --
    def _safe(self, rel: str) -> Path | None:
        if not rel or rel.startswith("/") or "\0" in rel or any(p == ".." for p in rel.split("/")):
            return None
        full = self.root / rel
        if not inside(self.root, Path(os.path.realpath(full.parent))):
            return None
        return full

    def _revert(self, xy: str, rel: str, is_orig: bool) -> bool:
        full = self._safe(rel)
        if full is None or "U" in xy or xy in ("DD", "AA"):
            return False
        if xy in ("??", "!!"):
            restored = self._ignored_backup.get(rel)
            if restored:
                kind, metadata, content, target = restored
                try:
                    if kind == "link" and target is not None:
                        if full.is_dir() and not full.is_symlink():
                            shutil.rmtree(full)
                        else:
                            full.unlink(missing_ok=True)
                        full.parent.mkdir(parents=True, exist_ok=True)
                        full.symlink_to(target)
                        return True
                    if content is not None:
                        if full.is_dir() and not full.is_symlink():
                            shutil.rmtree(full)
                        full.parent.mkdir(parents=True, exist_ok=True)
                        full.write_bytes(content)
                        if metadata is not None:
                            os.chmod(full, metadata.st_mode & 0o777)
                        return True
                    return False
                except OSError:
                    return False
            try:
                if os.path.islink(full) or full.is_file():
                    full.unlink()
                    return True
                if rel.endswith("/") and full.is_dir():
                    # Directory mtimes are not file identities. Inspect new leaves only.
                    if self.baseline and any(name.startswith(rel) for name in self.baseline):
                        for child in sorted(full.iterdir(), key=lambda p: p.name, reverse=True):
                            rel_child = child.relative_to(self.root).as_posix()
                            if not self.owned(rel_child) and rel_child not in self.baseline:
                                if child.is_dir() and not child.is_symlink():
                                    shutil.rmtree(child)
                                else:
                                    child.unlink()
                        return True
                    shutil.rmtree(full)
                    return True
            except OSError:
                pass
            return False
        if not is_orig and (xy[0] in "AC" or (xy[0] == "R")):
            self._git_fenced("rm", "-f", "--cached", "-q", "--", rel)
            try:
                if os.path.lexists(full):
                    full.unlink()
            except OSError:
                return False
            return True
        return self._git_fenced("checkout", "HEAD", "--", rel) == 0

    def reconcile(self) -> tuple[list[str], list[str]]:
        """Compare with the baseline. Revert new unowned changes. Return (reverted, errors)."""
        reverted: list[str] = []
        errors: list[str] = []
        now = self._rev()
        if now != self.head:
            if self.allow_commits:
                rc, out = self._git("diff", "-z", "--name-only", self.head, now)
                names = [n.decode("utf-8", "surrogateescape") for n in out.split(b"\0") if n]
                bad = [n for n in names if not self.owned(n)]
                if rc != 0 or bad:
                    errors.append("commit touches unowned path(s): " + ", ".join(bad or ["unknown"]))
                self.head = now
            else:
                branch = self._git("symbolic-ref", "-q", "HEAD")[1].decode().strip()
                rc = self._git_fenced("reset", "--soft", self.head) if branch == self.branch else 1
                if rc == 0:
                    reverted.append("HEAD")
                else:
                    errors.append("HEAD moved and could not be restored")
        try:
            entries = self._status()
        except GuardError as exc:
            return reverted, errors + [f"{exc}. Left for the dispatcher lease check"]
        for xy, path, orig in entries:
            for rel, is_orig in ((path, False), (orig, True)):
                if not rel or self.owned(rel):
                    continue
                known = self.baseline.get(rel)
                if known == (xy, self._signature(rel)):
                    continue
                if known is not None:
                    if self._revert(xy, rel, is_orig):
                        reverted.append(rel)
                    else:
                        errors.append(f"{rel} changed from its baseline and cannot be restored")
                elif self._revert(xy, rel, is_orig):
                    reverted.append(rel)
                else:
                    errors.append(f"{rel} could not be reverted safely")
        try:
            for xy, path, orig in self._status():
                for rel in (path, orig):
                    if rel and not self.owned(rel) and rel not in self.baseline:
                        msg = f"{rel} is still dirty after revert"
                        if msg not in errors and not any(e.startswith(rel + " ") for e in errors):
                            errors.append(msg)
        except GuardError:
            pass
        return reverted, errors


# ---- checkpoint and session helpers -------------------------------------------------------


def save_checkpoint(path: Path, messages: list[dict], turns: int, final_text: str,
                    secrets: Iterable[str] = ()) -> None:
    """Write one JSON checkpoint atomically. It holds history only. It holds no header and no key."""
    doc = {"messages": strip_marks(messages), "completed_turns": turns, "final_text": final_text,
           "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    text = redact(json.dumps(doc), secrets)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ckpt.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_checkpoint(path: Path) -> list[dict]:
    """Return the saved messages, or an empty list when the file is missing or invalid."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    msgs = doc.get("messages") if isinstance(doc, dict) else None
    return msgs if isinstance(msgs, list) else []


def _tool_text(value: str) -> str:
    return value if value else "(no output)"


# ---- the harness ---------------------------------------------------------------------------

CHILD_ENV = ("PATH", "HOME", "USER", "LANG", "TERM", "TZ", "TMPDIR")


class LiteHarness:
    """One agent loop over the raw messages endpoint with six fenced tools."""

    def __init__(self, *, workdir: Path, model: str, task_id: str, tracking: Any, owns: tuple[str, ...],
                 transport: Callable | None = None, session: list[dict] | None = None,
                 max_turns: int = 200, max_output_tokens: int = 16384, effort: str = "high",
                 timeout_sec: float = 1800.0, bash_max_sec: int = 1800, receipt: Path | None = None,
                 repo_context: str = "", frozen_contracts: str = "",
                 before_run: Callable[[], None] | None = None, before_turn: Callable[[], str] | None = None,
                 checkpoint: Path | None = None,
                 allowed_commits: bool = False, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.workdir = Path(workdir)
        self.root = Path(os.path.realpath(workdir))
        self.model, self.task_id, self.tracking, self.owns = model, task_id, tracking, tuple(owns)
        self.transport = transport or HttpTransport(timeout=timeout_sec)
        self.max_turns, self.max_output_tokens, self.effort = max_turns, max_output_tokens, effort
        self.timeout_sec, self.bash_max_sec = timeout_sec, bash_max_sec
        self.receipt = Path(receipt) if receipt else None
        self.system = build_system(repo_context, frozen_contracts)
        self.before_run, self.sleep, self.clock = before_run, sleep, clock
        self.before_turn = before_turn
        self.checkpoint = Path(checkpoint) if checkpoint else None
        if self.checkpoint is not None and inside(self.root, Path(os.path.realpath(self.checkpoint))):
            raise ValueError("checkpoint path must be outside the workspace")
        self._messages = strip_marks(session or [])
        self._seeded = bool(self._messages)
        self._ran = False
        self._secrets = _secrets(dict(os.environ))
        self.guard = BashGuard(self.root, self.owns, self._authorized, allowed_commits)
        self._shell_root = Path(tempfile.mkdtemp(prefix="jbi-lite-shell-"))
        try:
            from .private_git import PrivateGit
            self._private_git = PrivateGit(self.guard, self._shell_root)
        except Exception:
            shutil.rmtree(self._shell_root, ignore_errors=True)
            raise
        self._finalized = False
        self._shell_rejected: list[str] = []
        self.usage = Usage()

    def _finish(self, result: HarnessResult) -> HarnessResult:
        """Accept only valid owned private commits. Destroy all other private Git operations."""
        if self._finalized:
            return result
        error = None
        try:
            self._private_git.publish()
        except Exception as exc:
            error = f"ERROR: private Git publication refused: {redact(str(exc), self._secrets)}"
        finally:
            self._private_git.__exit__()
            self._finalized = True
            shutil.rmtree(self._shell_root, ignore_errors=True)
        if error:
            if result.exit_code:
                result.final_text += "\n" + error
            else:
                result.exit_code, result.final_text = 1, error
        return result

    # -- public --
    def take_messages(self) -> list[dict]:
        """Deep copy of the history with the cache marks of the next call. No state is shared."""
        return apply_cache_marks(self._messages)

    def run(self, brief: str) -> HarnessResult:
        result = HarnessResult()
        if self._seeded or self._ran:
            problem = self._check_before_run()
            if problem:
                result.exit_code, result.final_text = 1, f"ERROR: before_run refused: {problem}"
                return self._finish(result)
        self._messages.append({"role": "user", "content": build_user_content(brief, not self._messages)})
        self._ran = True
        deadline = self.clock() + self.timeout_sec
        last_text = ""
        try:
            for turn in range(1, self.max_turns + 1):
                message = self.before_turn() if self.before_turn else ""
                if message:
                    self._messages.append({"role": "user", "content": [{"type": "text", "text": message}]})
                if self.clock() >= deadline:
                    return self._fail(result, "timeout", last_text, timed_out=True, code=124)
                payload = build_payload(model=self.model, max_tokens=self.max_output_tokens,
                                        effort=self.effort, system=self.system, messages=self._messages)
                resp, err = self._call(payload, deadline)
                if err:
                    return self._fail(result, err[1], last_text, timed_out=err[0] == "timeout",
                                      code=124 if err[0] == "timeout" else 1)
                result.turns = turn
                self.usage = add_usage(self.usage, parse_usage(resp.get("usage")))
                result.usage = self.usage
                content = resp.get("content")
                blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
                texts = [str(b.get("text") or "") for b in blocks if b.get("type") == "text"]
                if any(t.strip() for t in texts):
                    last_text = "\n".join(t for t in texts if t.strip())
                uses = [b for b in blocks if b.get("type") == "tool_use"]
                if not blocks:
                    return self._fail(result, "model returned no content", last_text)
                if any(not isinstance(b.get("id"), str) or not b.get("id") for b in uses):
                    return self._fail(result, "tool_use block has no id. History not extended", last_text)
                self._messages.append({"role": "assistant", "content": copy.deepcopy(blocks)})
                self._save(turn, last_text)
                if resp.get("stop_reason") == "refusal":
                    return self._fail(result, "model refused the request", last_text)
                if not uses:
                    if resp.get("stop_reason") == "max_tokens":
                        return self._fail(result, "output truncated at max_tokens", last_text)
                    result.final_text = last_text
                    self._write_receipt(last_text)
                    return result
                results = [self._tool_result(b, deadline) for b in uses]
                self._messages.append({"role": "user", "content": results})
                self._save(turn, last_text)
            return self._fail(result, f"max_turns {self.max_turns} reached", last_text, code=2)
        finally:
            result.usage = self.usage
            self._finish(result)

    # -- internals --
    def _authorized(self, rel: str) -> bool:
        try:
            self.tracking.authorize_write(self.task_id, rel)
        except Exception:
            return False
        return True

    def _check_before_run(self) -> str | None:
        try:
            if self.before_run is not None:
                self.before_run()
                return None
            if self.guard._rev() != self.guard.head or self.guard.snapshot() != self.guard.baseline:
                return "workdir is not clean at its base commit"
            fleet = getattr(self.tracking, "fleet", None)
            for lease in (fleet()["leases"] if callable(fleet) else []):
                if (lease.get("released_at") is None and lease.get("task_id") != self.task_id
                        and os.path.realpath(lease.get("worktree", "")) == str(self.root)):
                    return f"task {lease.get('task_id')} holds a live lease on this workdir"
        except Exception as exc:
            return redact(str(exc), self._secrets)
        return None

    def _fail(self, result: HarnessResult, reason: str, last_text: str, *, timed_out: bool = False,
              code: int = 1) -> HarnessResult:
        text = f"ERROR: {redact(reason, self._secrets)}"
        if last_text:
            text += f"\nLast agent text:\n{last_text}"
        result.final_text, result.exit_code, result.timed_out = text, code, timed_out
        self._save(result.turns, text)
        return result

    def _save(self, turns: int, final_text: str) -> None:
        if self.checkpoint is not None:
            save_checkpoint(self.checkpoint, self._messages, turns, final_text, self._secrets)

    def _call(self, payload: dict, deadline: float) -> tuple[dict | None, tuple[str, str] | None]:
        """One model call with at most two retries. Returns (response, None) or (None, (kind, reason))."""
        for attempt in range(3):
            try:
                resp = self.transport(payload)
            except HttpError as exc:
                if exc.status in RETRYABLE_HTTP and attempt < 2:
                    if not self._wait(exc.retry_after, attempt, deadline):
                        return None, ("timeout", "deadline reached while waiting to retry")
                    continue
                return None, ("http", redact(f"HTTP {exc.status}: {exc.body}", self._secrets))
            except TimeoutError:
                if attempt < 2:
                    if not self._wait(None, attempt, deadline):
                        return None, ("timeout", "deadline reached while waiting to retry")
                    continue
                return None, ("timeout", "endpoint timed out")
            except Exception as exc:
                return None, ("protocol", redact(f"{type(exc).__name__}: {exc}", self._secrets))
            if not isinstance(resp, dict):
                return None, ("protocol", "endpoint answer is not an object")
            return resp, None
        return None, ("http", "retries exhausted")

    def _wait(self, retry_after: float | None, attempt: int, deadline: float) -> bool:
        delay = min(30.0, retry_after if retry_after is not None else 2.0 * (2 ** attempt))
        if self.clock() + delay >= deadline:
            return False
        self.sleep(delay)
        return True

    def _tool_result(self, block: dict, deadline: float) -> dict:
        name, inp = block.get("name"), block.get("input")
        if not isinstance(inp, dict):
            text = f"ERROR: tool input for {name!r} must be an object"
        elif name not in TOOL_NAMES:
            text = f"ERROR: unknown tool {name!r}. Use only: {', '.join(TOOL_NAMES)}"
        else:
            try:
                text = getattr(self, f"_t_{name}")(inp, deadline)
            except ToolError as exc:
                text = f"ERROR: {exc}"
            except Exception as exc:
                text = f"ERROR: {name} failed: {redact(f'{type(exc).__name__}: {exc}', self._secrets)}"
        out = {"type": "tool_result", "tool_use_id": block["id"], "content": _tool_text(text)}
        if text.startswith("ERROR"):
            out["is_error"] = True
        return out

    def _resolve(self, raw: Any) -> Path:
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            raise ToolError("bad path")
        p = Path(raw)
        target = Path(os.path.realpath(p if p.is_absolute() else self.root / p))
        if not inside(self.root, target):
            raise ToolError("path is outside the workspace")
        return target

    def _write_target(self, raw: Any) -> tuple[Path, str]:
        target = self._resolve(raw)
        rel = target.relative_to(self.root).as_posix()
        if rel == "." or rel == ".git" or rel.startswith(".git/"):
            raise ToolError("this path is not writable")
        try:
            self.tracking.authorize_write(self.task_id, rel)
        except Exception as exc:
            raise ToolError(f"write refused: {redact(str(exc), self._secrets)}") from None
        return target, rel

    def _put(self, target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not inside(self.root, Path(os.path.realpath(target.parent))):
            raise ToolError("path is outside the workspace")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)

    # -- the six tools, in fixed order --
    def _t_bash(self, inp: dict, deadline: float) -> str:
        cmd = inp.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ToolError("empty command")
        try:
            self.tracking.authorize_write(self.task_id, self.owns[0] if self.owns else "source", tool_write=False)
        except Exception as exc:
            raise ToolError(f"bash refused: {redact(str(exc), self._secrets)}") from None
        reason = self.guard.check(cmd)
        if reason:
            raise ToolError(f"command refused by the fence: {reason}")
        wanted = inp.get("timeout_sec")
        wanted = wanted if isinstance(wanted, int) and not isinstance(wanted, bool) and wanted > 0 else 180
        limit = max(1.0, min(float(wanted), float(self.bash_max_sec), deadline - self.clock()))
        env = fence_env()
        from .sandbox import fenced_argv
        argv, new_fence = fenced_argv(self.guard, cmd, self._private_git, self._shell_root)
        with new_fence as fence:
            proc = subprocess.Popen(fence.argv(cmd), cwd=str(self.root), env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                raw, _ = proc.communicate(timeout=limit)
                note = f"[exit_code={proc.returncode}]"
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                raw, _ = proc.communicate()
                note = f"[timed out after {limit:.0f}s]"
        out = raw.decode("utf-8", errors="replace")
        self._private_git.checkpoint()
        if proc.returncode != 0:
            self._shell_rejected.append("command failed after writing")
        if len(out) > BASH_OUT_MAX:
            out = out[:BASH_OUT_MAX // 2] + "\n...[truncated]...\n" + out[-BASH_OUT_MAX // 2:]
        reverted, errors = self.guard.reconcile()
        body = f"{note}\n{out}"
        if reverted or errors:
            detail = "; ".join(([f"reverted {', '.join(reverted)}"] if reverted else []) + errors)
            return f"ERROR: fence acted after the command: {detail}\n{body}"
        return body

    def _t_read_file(self, inp: dict, deadline: float) -> str:
        target = self._resolve(inp.get("path"))
        if not target.is_file():
            raise ToolError("file not found")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = inp.get("start_line")
        start = start if isinstance(start, int) and not isinstance(start, bool) and start >= 1 else 1
        end = inp.get("end_line")
        end = end if isinstance(end, int) and not isinstance(end, bool) else start + READ_MAX_LINES - 1
        end = min(end, len(lines), start + READ_MAX_LINES - 1)
        text = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
        return text[:READ_MAX_CHARS] if text else "(empty range)"

    def _t_write_file(self, inp: dict, deadline: float) -> str:
        content = inp.get("content")
        if not isinstance(content, str) or len(content) > 2_000_000:
            raise ToolError("content must be text of 2 MB or less")
        target, rel = self._write_target(inp.get("path"))
        self._put(target, content)
        return f"OK: wrote {len(content)} chars to {rel}"

    def _t_edit_file(self, inp: dict, deadline: float) -> str:
        target, rel = self._write_target(inp.get("path"))
        old, new = inp.get("old_text"), inp.get("new_text")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ToolError("old_text must be non-empty text and new_text must be text")
        if not target.is_file():
            raise ToolError("file not found")
        src = target.read_text(encoding="utf-8")
        count = src.count(old)
        if count != 1:
            raise ToolError(f"old_text matched {count} times in {rel}. It must match exactly once")
        self._put(target, src.replace(old, new, 1))
        return f"OK: edited {rel}"

    def _t_list_dir(self, inp: dict, deadline: float) -> str:
        target = self._resolve(inp.get("path") or ".")
        if not target.is_dir():
            raise ToolError("directory not found")
        names = [p for p in target.iterdir() if p.name != ".git" and not p.name.startswith(".ckpt")]
        names.sort(key=lambda p: (not p.is_dir(), p.name))
        lines = [f"{p.name}/" if p.is_dir() else f"{p.name} ({p.stat().st_size} B)" for p in names[:200]]
        return "\n".join(lines) or "(empty directory)"

    def _t_grep_files(self, inp: dict, deadline: float) -> str:
        pattern = inp.get("pattern")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 500:
            raise ToolError("pattern must be text of 1 to 500 characters")
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"bad pattern: {exc}") from None
        target = self._resolve(inp.get("path") or ".")
        files: list[Path] = []
        if target.is_file():
            files = [target]
        else:
            for base, dirs, names in os.walk(target, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                files += [Path(base) / n for n in sorted(names) if not n.startswith(".")]
                if len(files) > 2000:
                    break
        hits: list[str] = []
        for fp in files[:2000]:
            real = Path(os.path.realpath(fp))
            try:
                if not inside(self.root, real) or real.stat().st_size > 512_000:
                    continue
                data = real.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:1024]:
                continue
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{fp.relative_to(self.root).as_posix()}:{n}: {line[:220]}")
                    if len(hits) >= 60:
                        return "\n".join(hits)
        return "\n".join(hits) or "(no matches)"

    # -- receipt --
    def _write_receipt(self, final_text: str) -> None:
        """Write the receipt when the first line has the conventional shape. Never create a parent."""
        if self.receipt is None:
            return
        first = final_text.strip().splitlines()[0] if final_text.strip() else ""
        if not RECEIPT_LINE.match(first):
            return
        path = self.receipt
        if not path.is_absolute() or path.parent.is_symlink() or not path.parent.is_dir() or path.is_symlink():
            return
        try:
            self.tracking.authorize_write(self.task_id, f"receipts/{path.name}", tool_write=False)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(redact(final_text.strip()[:RECEIPT_MAX], self._secrets) + "\n")
        except Exception:
            return
