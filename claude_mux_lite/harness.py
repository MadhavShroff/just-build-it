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
                  messages: list[dict], tools: list[dict] | None = None) -> dict:
    payload = {"model": model, "max_tokens": max_tokens, "output_config": {"effort": effort},
               "system": system, "messages": apply_cache_marks(messages),
               "cache_control": dict(EPHEMERAL)}
    resolved_tools = TOOLS if tools is None else tools
    if resolved_tools:
        payload["tools"] = resolved_tools
    return payload


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
                     "pushd", "git", "tar", "unzip", "curl", "wget", "sort", "uniq", "iconv"})
WRAPPERS = frozenset({"env", "nohup", "nice", "time", "timeout", "exec", "command", "builtin", "xargs",
                      "stdbuf", "setsid", "taskset", "ionice", "chrt", "schedtool", "numactl", "setpriv",
                      "strace", "ltrace", "watch", "script", "parallel",
                      "prlimit", "setarch", "linux64", "flock", "choom", "capsh",
                      "unshare", "aa-exec", "nsenter", "eatmydata", "fakeroot", "dbus-run-session",
                      "ssh-agent", "systemd-cat"})
# Interpreter modules whose own arguments are code or code paths.
EXEC_MODULES = frozenset({"timeit", "runpy", "trace", "profile", "cProfile", "pdb", "doctest",
                          "code", "ensurepip", "pydoc"})
# Assignments that inject unchecked code or loaders into the interpreter.
INJECTION_VARS = frozenset({"BASH_ENV", "ENV", "PYTHONSTARTUP", "PYTHONHOME", "PERLLIB", "PERL5LIB",
                            "PERL5OPT", "NODE_OPTIONS", "NODE_PATH", "RUBYOPT", "RUBYLIB", "LD_PRELOAD",
                            "LD_LIBRARY_PATH", "ZDOTDIR"})
SYSTEM_BIN_PREFIXES = ("/bin/", "/usr/bin/", "/usr/sbin/", "/sbin/", "/usr/games/", "/snap/bin/")
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ash", "ksh", "mksh", "yash", "csh", "tcsh", "fish",
                    "rbash"})
# Writers whose execution can put content into a file the same command later runs.
EXEC_WRITERS = WRITERS - {"git"}


def _segment_writer(words: list[str]) -> bool:
    """True when the segment runs a worktree-writing program. A busybox prefix
    counts: `busybox tee x` writes just like `tee x`."""
    first = _first_command(words)
    if first == "busybox":
        rest = [w for w in words[_first_command_index(words) + 1:] if w != "--"]
        first = os.path.basename(rest[0]) if rest else ""
    return first in EXEC_WRITERS


def _first_command_index(words: list[str]) -> int:
    i = 0
    while i < len(words) and "=" in words[i] and not words[i].startswith("-"):
        i += 1
    return min(i, len(words))


INTERPRETERS = frozenset({"node", "perl", "ruby", "bash", "sh", "zsh", "php", "lua", "awk"})
KNOWN_COMMANDS = WRITERS | WRAPPERS | INTERPRETERS | SHELLS | {
    "git", "find", "tee", "mkdir", "rmdir", "busybox", ".", "source",
    "sudo", "su", "doas", "eval"}
SAFE_DEV = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "|&"})
REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", ">&", "<>"})
ABS_IN_CODE = re.compile(r"(?<![\w.:/])/(?!dev/(?:null|stdout|stderr)\b)[A-Za-z0-9_.~-]+")
DOTDOT_IN_CODE = re.compile(r"(?:^|[\s'\"=(,\[])\.\.(?:/|$|[\s'\")\],])")
STASH = re.compile(r"\bgit\b[^\n;&|]*\bstash\b")
FIND_TERM = ""  # stands in for the escaped or quoted `;` find terminator
FIND_PLACE = ""  # stands in for a `{}` argument inside a find exec payload


def _first_command(words: list[str]) -> str:
    """The basename of the first word that is not an assignment or a flag."""
    i = 0
    while i < len(words) and "=" in words[i] and not words[i].startswith("-"):
        i += 1
    if i >= len(words):
        return ""
    return os.path.basename(words[i])


def _writes_inside(root: Path, command: str) -> bool:
    """True when the command string writes into the worktree: redirects,
    executing writers, or nested shell -c strings that write. It detects the
    writes an outer script or module read must not trust."""
    try:
        lex = shlex.shlex(_token_text(command), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return True
    for j, tok in enumerate(tokens):
        if tok in REDIRECTS and j + 1 < len(tokens):
            target = tokens[j + 1]
            if (tok == ">&" and (target.isdigit() or target == "-")) or target in SAFE_DEV:
                continue
            stripped = target.replace("${TMPDIR}", "").replace("$TMPDIR", "")
            if not stripped:
                continue
            probe = Path(stripped if stripped.startswith("/") else root / stripped)
            if inside(root, Path(os.path.realpath(probe))):
                return True
    for words in _split_segments(tokens):
        if _segment_writer(words):
            return True
        inner = _inner_command_string(words)
        if inner is not None and _writes_inside(root, inner):
            return True
    return False


def _inner_command_string(words: list[str]) -> str | None:
    """The command string a shell segment carries with -c, if any."""
    for k, word in enumerate(words):
        if word == "-c" or re.fullmatch(r"-[A-Za-z0-9]*c", word):
            if k + 1 < len(words) and not words[k + 1].startswith("-"):
                return words[k + 1]
    return None


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segment: list[str] = []
    segments: list[list[str]] = []
    for tok in tokens:
        if tok in SEPARATORS:
            segments.append(segment)
            segment = []
        elif not set(tok) <= set("<>&|;()"):
            segment.append(tok)
    segments.append(segment)
    return segments


def _token_text(command: str) -> str:
    text = _plain_newlines(command)
    for pattern in ("\\;", "';'", '";"'):
        text = text.replace(pattern, FIND_TERM)
    return text


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


def _augment_user_path(raw_path: str, home: str) -> str:
    """Include standard user tool directories (local bin, cargo, nvm node) when present."""
    if not home:
        return raw_path
    parts = [p for p in raw_path.split(":") if p]
    seen = set(parts)
    extra: list[str] = []
    home_path = Path(home)
    nvm_dir = home_path / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        try:
            versions = sorted((p / "bin" for p in nvm_dir.iterdir() if (p / "bin").is_dir()), reverse=True)
            if versions:
                extra.append(str(versions[0]))
        except OSError:
            pass
    for rel in (".local/bin", ".cargo/bin"):
        cand = home_path / rel
        if cand.is_dir():
            extra.append(str(cand))
    prepend = [p for p in extra if p not in seen]
    return ":".join(prepend + parts) if prepend else raw_path


def fence_env() -> dict[str, str]:
    """The environment of a fenced shell. It holds no credential."""
    env = {k: os.environ[k] for k in CHILD_ENV if k in os.environ}
    env["PATH"] = _augment_user_path(env.get("PATH", "/usr/bin:/bin"), env.get("HOME", ""))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def host_git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in CHILD_ENV or k.startswith("LC_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    return env


class GuardError(Exception):
    """The fence cannot parse or cannot safely repair a state."""


def _plain_newlines(cmd: str) -> str:
    """Replace unquoted newlines with ';'. Quotes and backslashes keep their
    meaning. A '#' outside quotes comments to end of line, and the newline
    after it still separates commands."""
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
        elif ch == "#":
            while i < len(cmd) and cmd[i] != "\n":
                i += 1
            continue
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

    def _status(self, expand_ignored: bool = True) -> list[tuple[str, str, str | None]]:
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
        entries = parse_status(out) + ignored
        # Reconcile needs every ignored leaf. The snapshot expansion reads the
        # compact aggregates itself, because it also backs the leaves up.
        return self._ignored_entries(entries) if expand_ignored else entries

    def _ignored_entries(self, entries: list[tuple[str, str, str | None]]) -> list[tuple[str, str, str | None]]:
        """Replace every non-empty ignored directory with its leaves.

        Reconcile judges the leaves alone. A leftover aggregate would let
        reconcile delete owned leaves under a glob own, or double-revert the
        directory after its leaves are judged."""
        current = {rel: (xy, rel, orig) for xy, rel, orig in entries}
        for xy, rel, original in entries:
            if xy != "!!" or not rel.endswith("/"):
                continue
            if rel == "node_modules/" or rel.endswith("/node_modules/"):
                continue
            rc, raw = self._git("ls-files", "-o", "-i", "--exclude-standard", "-z", "--", rel)
            if rc != 0:
                raise GuardError("ignored baseline discovery failed")
            children = [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item]
            for child in children:
                if child != rel:
                    current[child] = ("!!", child, None)
            if children:
                current.pop(rel, None)
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
        entries = self._status(expand_ignored=False)
        for xy, path, orig in entries:
            for p in (path, orig):
                if p:
                    snap[p] = (xy, self._signature(p))
        directories = [path for xy, path, _ in entries
                       if xy == "!!" and path.endswith("/")
                       and path != "node_modules/" and not path.endswith("/node_modules/")]
        for directory in directories:
            # Enumerate real ignored leaves, not the aggregate directory status.
            rc, raw = self._git("ls-files", "-o", "-i", "--exclude-standard", "-z", "--", directory)
            if rc != 0:
                raise GuardError("ignored baseline discovery failed")
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
        if any(part == ".." for part in arg.split("/")):
            return f"parent traversal in {arg!r}"
        if arg in SAFE_DEV or arg == "$TMPDIR" or arg.startswith(("$TMPDIR/", "${TMPDIR}/")):
            return None
        target = Path(os.path.realpath(arg if arg.startswith("/") else self.root / arg))
        if not inside(self.root, target):
            return f"{arg!r} is outside the workspace"
        return None

    def _path_taints(self, words: list[str]) -> bool:
        """True when the segment puts the command search path under the worktree
        or into an expansion: PATH= assignments and export-style PATH statements."""
        for word in words:
            if word.startswith("PATH="):
                value = word.split("=", 1)[1]
                if "$" in value:
                    return True
                for part in value.split(":"):
                    if part and inside(self.root, Path(os.path.realpath(self.root / part))):
                        return True
        for k, word in enumerate(words):
            # printf -v PATH and read PATH set the search variable without an
            # assignment word, and the shell honours it either way.
            if word in ("-v", "-p", "-r", "-a") and k + 1 < len(words) and words[k + 1] == "PATH":
                return True
        return False

    def _env_taints(self, words: list[str]) -> bool:
        """True when the segment points the module search into agent-writable
        space: a PYTHONPATH assignment with an expansion or a worktree path,
        or an export-style statement naming PYTHONPATH without a value."""
        for word in words:
            if word.startswith("PYTHONPATH=") and self._pythonpath_taints(word):
                return True
        if _first_command(words) in ("export", "declare", "readonly", "local", "setenv", "typeset"):
            for word in words[1:]:
                if word == "PYTHONPATH" or word.startswith("PYTHONPATH="):
                    return True
                if word.startswith("PATH="):
                    value = word.split("=", 1)[1]
                    if "$" in value:
                        return True
                    for part in value.split(":"):
                        if part and inside(self.root, Path(os.path.realpath(self.root / part))):
                            return True
        if _first_command(words) == "set" and (any(re.fullmatch(r"-[A-Za-z]*a", a) for a in words[1:])
                                               or "allexport" in words[1:]):
            return True
        return False

    def _pythonpath_taints(self, word: str) -> bool:
        """True when a PYTHONPATH assignment points the module search into
        agent-writable space: any expansion, or a directory inside the worktree."""
        if not word.startswith("PYTHONPATH="):
            return False
        value = word.split("=", 1)[1]
        if "$" in value:
            return True
        for part in value.split(":"):
            if part and inside(self.root, Path(os.path.realpath(self.root / part))):
                return True
        return False

    def _segment(self, words: list[str], script_ok: bool = True,
                 env_hostile: bool = False, path_hostile: bool = False) -> str | None:
        for word in words:
            if any(ch in word for ch in "`~"):
                return "expanded shell commands and command options are refused"
            safe = re.sub(r'(?<!\\)\$\{?TMPDIR\}?(?:/[\w.-]+)*', "", word)
            # Shell positional and status parameters carry no path.
            safe = re.sub(r'\$\{[0-9@*#?\-]\}|\$[0-9@*#?\-]', "", safe)
            if "$" in safe:
                return "expanded shell commands and command options are refused"
        i = 0
        while i < len(words) and "=" in words[i] and not words[i].startswith("-"):
            i += 1
        # Assignment-only segments still set the environment later segments run with.
        for k in range(i):
            name = words[k].split("=", 1)[0]
            if name in INJECTION_VARS:
                return f"{name} injection is refused"
        if i >= len(words):
            return None
        cmd, args = os.path.basename(words[i]), words[i + 1:]
        if cmd == FIND_PLACE:
            return "a find exec section cannot run the found path as code"
        if cmd in ("alias", "unalias", "shopt", "fc"):
            return "shell aliases and history re-execution are refused"
        if cmd == "make" and any(a in ("-f", "--file", "--makefile") or a.startswith("--file=")
                                 for a in args):
            return "make files run unchecked recipes"
        if cmd == "tar" and any("checkpoint-action" in a for a in args):
            return "tar checkpoint actions run commands"
        if cmd in ("export", "declare", "readonly", "typeset", "local", "setenv"):
            for arg in args:
                name = arg.split("=", 1)[0].lstrip("-")
                if name in INJECTION_VARS:
                    return f"{name} injection is refused"
        if os.path.basename(words[i]) in WRAPPERS:
            return "command wrappers are refused"
        if any(ch in words[i] for ch in "*?[{"):
            return "the command word may glob or expand into another program"
        if "$" in words[i] or "`" in words[i]:
            return "the command word may expand into an unchecked path"
        if cmd.startswith("ld-linux") or cmd.startswith("ld-musl") or cmd == "ld.so":
            return "the dynamic loader is refused"
        if cmd in ("sudo", "su", "doas", "eval"):
            return "privilege escalation is refused"
        if cmd in (".", "source"):
            return "shell sourcing is refused"
        if cmd in ("trap", "mapfile", "readarray", "coproc"):
            return "shell callbacks are refused"
        if path_hostile and "/" not in words[i]:
            return "the command search path is tainted"
        if cmd == "set" and (any(re.fullmatch(r"-[A-Za-z]*a", a) for a in args)
                             or "allexport" in args):
            return "shell allexport is refused"
        if cmd == "cmake" and "-E" in args:
            return "cmake -E command runners are refused"
        for j in range(i):
            name_value = words[j].split("=", 1)
            if name_value[0] in INJECTION_VARS:
                return f"{name_value[0]} injection is refused"
            if name_value[0] == "PATH":
                value = name_value[1]
                if "$" in value:
                    return "the command search path may not expand"
                for part in value.split(":"):
                    if part and inside(self.root, Path(os.path.realpath(self.root / part))):
                        return "the command search path may not point into the workspace"
        resolved = Path(os.path.realpath(self.root / words[i]))
        if "/" in words[i] and inside(self.root, resolved):
            return "direct execution of a workspace path is unchecked; run the interpreter"
        if "/" in words[i] and cmd not in KNOWN_COMMANDS and resolved.exists() \
                and not str(resolved).startswith(SYSTEM_BIN_PREFIXES):
            return "direct script execution is unchecked; run the interpreter"
        if cmd in ("mkdir", "rmdir"):
            for arg in (a for a in args if not a.startswith("-")):
                bad = self._bad_path(arg)
                rel = os.path.relpath(os.path.realpath(self.root / arg), self.root) if not bad else ""
                scratch = arg.startswith(("$TMPDIR", "${TMPDIR}"))
                if bad or (not scratch and not self.owned(rel)):
                    return f"{cmd} target refused: {bad or arg + ' is not leased'}"
        if cmd == "tee":
            for arg in (a for a in args if not a.startswith("-")):
                bad = self._bad_path(arg)
                if bad:
                    return f"tee target refused: {bad}"
        if cmd == "git":
            if any(any(ch in a for ch in "$`~") for a in args):
                return "expanded Git options and verbs are refused"
            if any(argument in ("-c", "--config-env") or argument.startswith(("-c", "--config-env="))
                   for argument in args):
                return "Git configuration is reserved for the runtime"
            forbidden = ("--exec-path", "--work-tree", "--git-dir", "-C",
                         "--exec-path=", "--work-tree=", "--git-dir=")
            if any(arg in ("--namespace", "--super-prefix") or arg.startswith(("--namespace=", "--super-prefix="))
                   for arg in args):
                return "Git repository selection is reserved for the runtime"
            if not args or any(a == flag or (flag.endswith("=") and a.startswith(flag))
                               for flag in forbidden for a in args):
                return "Git directory and execution options are reserved for the runtime"
            verb = 0
            while verb < len(args) and args[verb].startswith("-"):
                if args[verb] not in ("--no-pager", "-p"):
                    return "Git global options are confined to the runtime"
                verb += 1
            if verb >= len(args):
                return "a Git subcommand is required"
            subcommand = args[verb] if verb is not None else ""
            gargs = [a for a in args[verb:] if not a.startswith("-")]
            if any(a == "--output" or a.startswith("--output=") for a in args):
                return "git output redirection is refused"
            read_commands = {"add", "commit", "diff", "log", "show", "status", "ls-files", "rev-list",
                             "rev-parse", "cat-file", "checkout", "grep", "rm", "blame",
                             "describe", "shortlog", "show-ref", "for-each-ref", "branch", "reset", "clean"}
            if subcommand == "clean":
                return "git clean is refused because deletion is not recoverable by the task transaction"
            if subcommand not in read_commands:
                return f"git {subcommand or '<unspecified>'} is confined to the private task index"
            if subcommand == "branch" and (not gargs or len(gargs) > 1 or any(a.startswith("-") for a in args[verb + 1:])):
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
            if subcommand == "reset" and any(a not in ("HEAD", "--") for a in args[verb + 1:]):
                return "a shell reset may restore only HEAD paths"
            if subcommand == "checkout" and any(
                    (a.startswith("-") or re.fullmatch(r"[0-9a-fA-F]{4,64}", a))
                    and a not in ("HEAD", "--") for a in args[verb + 1:]):
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
            if any(a in ("-delete", "-fprint", "-fprintf", "-fls", "-fprint0") for a in args):
                return "find deletion and file writes are refused"
            index = 0
            while index < len(args):
                if args[index] in ("-exec", "-execdir", "-ok", "-okdir"):
                    payload: list[str] = []
                    cursor = index + 1
                    closed = False
                    while cursor < len(args):
                        if args[cursor] in (";", "+", FIND_TERM):
                            closed = True
                            cursor += 1
                            break
                        payload.append(FIND_PLACE if args[cursor] == "{}" else args[cursor])
                        cursor += 1
                    if not closed:
                        return "a find exec section needs a closing ; or +"
                    if not payload or payload[0] == FIND_PLACE:
                        return "a find exec section needs a command to run"
                    reason = self._segment(payload, script_ok=script_ok, env_hostile=env_hostile,
                                           path_hostile=path_hostile)
                    if reason:
                        return f"find exec is refused: {reason}"
                    index = cursor
                    continue
                index += 1
            for arg in (a for a in args if not a.startswith("-") and a not in ("(", ")", "!", "{}")):
                if "/" in arg or arg == "..":
                    bad = self._bad_path(arg)
                    if bad:
                        return f"find target refused: {bad}"
        elif cmd == "sed" or cmd in WRITERS:
            if cmd == "sed":
                scripts = [a for a in args if not a.startswith("-")]
                scripts += [a[2:] for a in args if a.startswith("-e") and len(a) > 2]
                for script in scripts:
                    if FIND_PLACE in script:
                        return "a find exec section cannot feed the found path to sed"
                    if re.search(r"(?:^|;)\s*(?:[0-9$~,]*|/[^/\n]*/)[!]*[0-9]*e(?=[\s;]|$)", script) \
                            or re.search(r"s(.)[^=\n]*\1[^=\n]*\1[^;\n]*e(?=[^a-z]|$)", script):
                        return "sed e executes a command and is refused"
                flag = next((a for a in args if a in ("-f", "--file")), None)
                if flag is not None:
                    file_arg = args[args.index(flag) + 1] if args.index(flag) + 1 < len(args) else ""
                    if file_arg == FIND_PLACE or file_arg.startswith(("$TMPDIR", "${TMPDIR}")) \
                            or (file_arg and not file_arg.startswith("-")
                                and not inside(self.root, Path(os.path.realpath(
                                    self.root / file_arg)))):
                        return "sed script files may not come from outside the workspace"
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
        if cmd == "busybox" and args:
            # The multi-call binary reaches every applet, including shells.
            rest = args[1:] if args[0] == "--" else args
            if rest and rest[0] == FIND_PLACE:
                return "a find exec section needs a command to run"
            return self._segment(rest, script_ok=script_ok, env_hostile=env_hostile,
                                 path_hostile=path_hostile)
        if cmd in SHELLS:
            positionals = [a for a in args if not a.startswith("-")]
            command_flag = next((a for a in args if a == "-c" or re.fullmatch(r"-[A-Za-z0-9_]*c", a)), None)
            if command_flag is not None:
                head = args[:args.index(command_flag)]
                if any(re.fullmatch(r"-[A-Za-z0-9_]*s[A-Za-z0-9_]*", a) for a in head):
                    return "a shell may read commands from standard input"
                if any(a == "-o" or a.startswith("--") for a in head):
                    return "shell options are refused"
                if any(not a.startswith("-") for a in head):
                    return "a shell may not run a script before its command string"
                after = args[args.index(command_flag) + 1:]
                # Words after the string become $0 and positional data; the
                # string alone is the code the shell runs.
                if not after or after[0].startswith("-"):
                    return "a shell command string needs exactly one checked word"
                return self.check(after[0], _script_ok=script_ok, _env_hostile=env_hostile,
                                  _path_hostile=path_hostile)
            if any(re.fullmatch(r"-[A-Za-z0-9_]*s[A-Za-z0-9_]*", a) for a in args):
                return "a shell may read commands from standard input"
            if any(a == "-o" for a in args):
                return "shell option flags are refused"
            if any(a.startswith("--") for a in args):
                return "shell long options are refused"
            if not script_ok:
                return "a shell script may not follow a writing segment"
            if not positionals:
                return "a bare shell interpreter may read unchecked input"
            if len(positionals) > 1:
                return "a shell script may take no positional arguments"
            script = self._safe(positionals[0])
            if script is None or not script.is_file():
                return "shell script cannot be read safely"
            return self.check(script.read_text(encoding="utf-8", errors="replace"),
                              _script_ok=script_ok, _env_hostile=env_hostile,
                              _path_hostile=path_hostile)
        if cmd.startswith("python") or cmd in INTERPRETERS:
            # Attached forms (`-c"code"`) tokenize as one word, so the prefix
            # carries the check. A bare interpreter executes piped input, and a
            # script argument is unchecked content. The module name after `-m`
            # is the only non-flag word a module form may carry.
            module_flag = next((a for a in args if a == "-m"), None)
            head = args[:args.index(module_flag)] if module_flag is not None else args
            if any(not a.startswith("-") for a in head):
                return "interpreter script files cannot be checked; use shell commands"
            inline_prefixes = ("-c", "-e", "-p", "-r", "-f", "--eval", "--print", "--source",
                               "--file", "--exec", "--include")
            scan_args = head if module_flag is not None else args
            if any(a == "-" or a.startswith(inline_prefixes) for a in scan_args):
                return "interpreter inline code cannot be checked"
            if any((m := re.match(r"-([A-Za-z]+)", a)) and set(m.group(1)) & set("cepr") for a in scan_args):
                return "interpreter inline code cannot be checked"
            if not script_ok:
                return "an interpreter module may not follow a writing segment"
            if env_hostile:
                return "an interpreter module path may not point into agent-writable space"
            if module_flag is not None:
                after = args[args.index(module_flag) + 1:]
                if not after or not re.fullmatch(r"[A-Za-z_][\w.]*", after[0]):
                    return "an interpreter module must be one plain identifier"
                if after[0] in EXEC_MODULES:
                    return "that interpreter module executes code strings"
                for word in after[1:]:
                    if any(ch in word for ch in "()[]{};'\"$"):
                        return "interpreter module arguments cannot carry code or expansions"
                    if word.startswith("/"):
                        return "interpreter module arguments may not name absolute paths"
                    if not word.startswith("-"):
                        stem = self.root / word.replace(".", "/")
                        if word.endswith(".py") and (self.root / word).exists():
                            return "an interpreter module argument may not name a workspace file"
                        if "." in word and re.fullmatch(r"[A-Za-z_][\w.]*", word) \
                                and (stem.with_suffix(".py").exists()
                                     or (stem / "__init__.py").exists()):
                            return "an interpreter module argument may not name a workspace module"
                stem = self.root / after[0].replace(".", "/")
                if stem.with_suffix(".py").exists() or (stem / "__init__.py").exists() \
                        or (stem / "__main__.py").exists():
                    return "an interpreter module must not resolve inside the workspace"
                return None
            return "an interpreter without a script or module may read unchecked input"
        return None

    def check(self, command: str, _script_ok: bool | None = None,
              _env_hostile: bool | None = None, _path_hostile: bool | None = None) -> str | None:
        """Return None when the command passes the fence, else the refusal reason."""
        if STASH.search(command):
            return "git stash is refused"
        try:
            if re.search(r"<<-?\s*['\"]?", command):
                return "shell heredocs are refused"
            text = _plain_newlines(command)
            for pattern in ("\\;", "';'", '";"'):
                text = text.replace(pattern, FIND_TERM)
            lex = shlex.shlex(text, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            tokens = list(lex)
        except ValueError:
            return "command is unparseable"
        segment: list[str] = []
        segments: list[list[str]] = []
        redirect_seen = False
        skip_target = False
        for i, tok in enumerate(tokens):
            if skip_target:
                skip_target = False
                continue
            if tok in REDIRECTS and i + 1 < len(tokens):
                target = tokens[i + 1]
                skip_target = True
                if segment and segment[-1].isdigit():
                    segment.pop()
                if not (tok == ">&" and (target.isdigit() or target == "-")):
                    if target not in SAFE_DEV:
                        stripped = target.replace("${TMPDIR}", "").replace("$TMPDIR", "")
                        if stripped:
                            probe = Path(stripped if stripped.startswith("/") else self.root / stripped)
                            redirect_seen = redirect_seen or inside(self.root,
                                                                    Path(os.path.realpath(probe)))
                    bad = self._bad_path(target)
                    if bad:
                        return f"redirect refused: {bad}"
                continue
            if tok in SEPARATORS:
                segments.append(segment)
                segment = []
            elif set(tok) <= set("<>&|;()"):
                # Coalesced punctuation such as `);` or `)>` never splits or
                # checks, so it cannot pass as a word.
                if tok not in REDIRECTS:
                    return "command is unparseable"
            else:
                segment.append(tok)
        segments.append(segment)
        # A script or module read is only safe when nothing in the same command
        # can rewrite the worktree between the read here and the run. A write
        # hidden inside a nested shell -c string counts, and so does a write
        # by a redirected segment. /dev/null, fd dups, and scratch targets do
        # not taint.
        nested_writes = False
        for words in segments:
            inner = _inner_command_string(words)
            if inner is not None and _writes_inside(self.root, inner):
                nested_writes = True
        writes = redirect_seen or nested_writes \
            or any(_segment_writer(w) for w in segments)
        script_ok = (_script_ok if _script_ok is not None else True) and not writes
        env_hostile = _env_hostile or any(self._env_taints(words) for words in segments)
        path_hostile = _path_hostile or any(self._path_taints(words) for words in segments)
        for words in segments:
            reason = self._segment(words, script_ok=script_ok, env_hostile=env_hostile,
                                   path_hostile=path_hostile)
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
                    base = rel.rstrip("/")
                    protected = any(pattern == base or pattern.startswith(base + "/")
                                    for pattern in self.owns)
                    if protected:
                        return False
                    if self.baseline and any(name.startswith(rel) for name in self.baseline):
                        for child in full.iterdir():
                            rel_child = child.relative_to(self.root).as_posix()
                            reserved = any(name == rel_child or name.startswith(rel_child + "/")
                                           for name in self.baseline)
                            leased = (self.owned(rel_child)
                                      or any(pattern == rel_child or pattern.startswith(rel_child + "/")
                                             for pattern in self.owns))
                            if not leased and not reserved:
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
                if rel.endswith("/") and (any(name.startswith(rel) for name in self.baseline)
                                          or any(p == rel.rstrip("/") or p.startswith(rel.rstrip("/") + "/")
                                                 for p in self.owns)):
                    # An aggregate ignored-directory entry changes signature when any
                    # leaf appears. Its leaves and owned descendants are judged alone.
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
                    # A directory aggregate is judged through its enumerated leaves.
                    if rel and not rel.endswith("/") and not self.owned(rel) and rel not in self.baseline:
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
                 on_progress: Callable[[str], None] | None = None,
                 checkpoint: Path | None = None,
                 wrap_up_final_turn: bool = False,
                 allowed_commits: bool = False, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.workdir = Path(workdir)
        self.root = Path(os.path.realpath(workdir))
        self.model, self.task_id, self.tracking, self.owns = model, task_id, tracking, tuple(owns)
        self.transport = transport or HttpTransport(timeout=timeout_sec)
        self.max_turns, self.max_output_tokens, self.effort = max_turns, max_output_tokens, effort
        self.wrap_up_final_turn = wrap_up_final_turn
        self.timeout_sec, self.bash_max_sec = timeout_sec, bash_max_sec
        self.receipt = Path(receipt) if receipt else None
        self.system = build_system(repo_context, frozen_contracts)
        self.before_run, self.sleep, self.clock = before_run, sleep, clock
        self.before_turn = before_turn
        self.on_progress = on_progress
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
                final_wrap_up = self.wrap_up_final_turn and self.max_turns >= 4 and turn == self.max_turns
                if final_wrap_up:
                    self._messages.append({"role": "user", "content": [{"type": "text", "text": (
                        "Final turn reached: do not call any more tools. Emit your complete "
                        "BRIEF <task> | <sha-or-uncommitted> | PASS or FAIL receipt now based on the evidence gathered."
                    )}]})
                if self.on_progress is not None:
                    self.on_progress(f"[jbi.lite] task={self.task_id} turn={turn} model={self.model} request")
                payload = build_payload(model=self.model, max_tokens=self.max_output_tokens,
                                        effort=self.effort, system=self.system, messages=self._messages,
                                        tools=[] if final_wrap_up else None)
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
                    if self.on_progress is not None:
                        self.on_progress(f"[jbi.lite] task={self.task_id} turn={turn} done "
                                         f"in={self.usage.input_tokens} out={self.usage.output_tokens} "
                                         f"cache_read={self.usage.cache_read}")
                    return result
                if self.on_progress is not None:
                    tool_summary = ",".join(str(b.get("name") or "?") for b in uses)
                    self.on_progress(f"[jbi.lite] task={self.task_id} turn={turn} tools={tool_summary}")
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
        reverted = [r for r in reverted if r != ".yarn/install-state.gz" and not r.endswith("/.yarn/install-state.gz")]
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
