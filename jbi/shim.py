"""The per-agent wrapper: `python3 -m jbi.shim <spec.json>`. It runs inside a tmux window.

The shim starts the agent in a new session (its own process group), tees the agent's
output to the window and to a log file, and keeps a JSON status file up to date. It
ties the agent's life to the CLI process:

- When the parent CLI process is gone, it sends SIGTERM and then SIGKILL to the group.
- SIGTERM or SIGHUP to the shim is forwarded to the group, with the same escalation.
- SIGINT to the shim is forwarded as SIGINT only: a graceful interrupt.
- When the agent leader exits, any process it left behind in its group is killed.

Spec keys: cmd (list), cwd, provider, env (extra variables), parent_pid, parent_start
(optional /proc start time), status (path), log (path), grace (seconds, optional).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .agents import Usage, child_env, parse_usage
from .store import now

POLL_SECONDS = 1.0
DEFAULT_GRACE = 5.0


def process_start(pid: int) -> str | None:
    """The kernel start time of `pid` (Linux), used to tell a live parent from a reused pid."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]


def alive(pid: int, start: str | None = None) -> bool:
    """True when `pid` exists and, when `start` is known, is the same process as before."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return start is None or process_start(pid) in (None, start)


def _stored_key_env(provider: str | None) -> dict[str, str]:
    """The provider's stored API key, read here from the keychain, never from the spec file.

    The spec file is on disk and the tmux server is shared, so neither may carry a key.
    A key kept in memory (no keychain) lives in the CLI process only and cannot reach here.
    """
    if not provider:
        return {}
    try:
        from .providers import child_key_env
        return child_key_env(provider)
    except Exception:            # a keychain failure must not stop the agent; it runs on its own login
        return {}


class Groups:
    """Process groups that the runtime started directly (acceptance, lane and bootstrap checks).

    A pgid stays registered only while its leader is unreaped, so `signal_all` can never
    reach an unrelated process that reused the id. The lock covers signal and removal."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: set[int] = set()
        self.seen: list[int] = []

    def add(self, pgid: int) -> None:
        with self._lock:
            self._live.add(pgid)
            self.seen.append(pgid)

    def discard(self, pgid: int) -> None:
        with self._lock:
            self._live.discard(pgid)

    def signal_all(self, sig: int) -> None:
        with self._lock:
            for pgid in list(self._live):
                if pgid not in (os.getpid(), os.getpgrp()):
                    _killpg(pgid, sig)


@dataclass(frozen=True)
class CompletedResult:
    returncode: int | None          # None when the command timed out
    output: str
    timed_out: bool = False


COMMAND_GRACE = 1.0

# The watchdog runs as its own process, so it outlives a SIGKILL of the parent. It ends
# quietly when the runner writes "done"; on EOF or a dead parent it kills the group.
_WATCHDOG = r"""
import os, select, signal, sys, time
parent, start, pgid, grace = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
def alive():
    try:
        os.kill(parent, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if start == "-":
        return True
    try:
        return open(f"/proc/{parent}/stat").read().rsplit(")", 1)[1].split()[19] == start
    except OSError:
        return False
while alive():
    if select.select([0], [], [], 0.2)[0]:
        if os.read(0, 16).startswith(b"done"):
            sys.exit(0)
        break
for sig in (signal.SIGTERM, signal.SIGKILL):
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass
    time.sleep(grace)
"""

try:                                            # resolve prctl before any fork
    import ctypes
    _PRCTL = ctypes.CDLL(None, use_errno=True).prctl if sys.platform.startswith("linux") else None
except (OSError, AttributeError):
    _PRCTL = None


def _child_guard() -> None:
    """preexec: the kernel SIGKILLs the child when the forking thread or process dies."""
    if _PRCTL is not None:
        _PRCTL(1, int(signal.SIGKILL), 0, 0, 0)   # PR_SET_PDEATHSIG


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _watch_thread(parent: int, start: str | None, pgid: int, grace: float, done: threading.Event) -> None:
    while not done.is_set():
        if not alive(parent, start):
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if done.is_set():
                    return
                _killpg(pgid, sig)
                done.wait(grace)
            return
        done.wait(0.2)


def run_command(command: str, *, cwd: Path, env: dict, timeout: float, parent_pid: int | None = None,
                groups: Groups | None = None, grace: float = COMMAND_GRACE) -> CompletedResult:
    """Run one shell command in its own process group, tied to `parent_pid`. Never raises."""
    parent = int(parent_pid or os.getpid())
    start = process_start(parent)
    try:
        proc = subprocess.Popen(command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                                preexec_fn=_child_guard)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return CompletedResult(127, f"cannot start the command: {error}")
    if groups is not None:
        groups.add(proc.pid)
    done = threading.Event()
    threading.Thread(target=_watch_thread, args=(parent, start, proc.pid, grace, done), daemon=True).start()
    try:
        watchdog = subprocess.Popen([sys.executable, "-I", "-c", _WATCHDOG, str(parent), start or "-",
                                     str(proc.pid), str(grace)], stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        watchdog = None
    chunks: list[bytes] = []
    reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()), daemon=True)
    reader.start()
    deadline, timed_out = time.monotonic() + timeout, False
    try:
        while True:
            try:
                if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None:
                    break
            except ChildProcessError:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _killpg(proc.pid, signal.SIGKILL)
            time.sleep(0.05)
        _killpg(proc.pid, signal.SIGKILL)       # the leader is unreaped: the pgid is still ours
    finally:
        if groups is not None:
            groups.discard(proc.pid)
        done.set()
        code = proc.wait()
        reader.join(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()
        if watchdog is not None:
            try:
                watchdog.communicate(b"done", timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                watchdog.kill()
                watchdog.wait()
    out = b"".join(c for c in chunks if c).decode("utf-8", "replace")
    return CompletedResult(None if timed_out else code, out, timed_out)


def write_json(path: Path, data: dict) -> None:
    """Write JSON atomically, so a reader never sees half a file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError as error:                    # the run dir is gone; the agent is still stopped
        print(f"[jbi.shim] cannot write {path}: {error}", file=sys.stderr)


class Shim:
    def __init__(self, spec: dict):
        self.spec = spec
        self.status_path = Path(spec["status"])
        self.grace = float(spec.get("grace", DEFAULT_GRACE))
        self.usage = Usage()
        self.proc: subprocess.Popen | None = None
        self.status: dict = {"shim_pid": os.getpid(), "shim_start": process_start(os.getpid()),
                             "pid": None, "pgid": None, "start": None, "started": None,
                             "ended": None, "exit_code": None, "usage": None, "reason": None}
        self._lock = threading.RLock()          # re-entrant: signal handlers run on the main thread
        self._reaped = False
        self._escalation: threading.Timer | None = None

    # ---- process group control ----------------------------------------------------

    def signal_group(self, sig: int) -> None:
        with self._lock:
            if self.proc is None or self._reaped:
                return
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                pass

    def terminate(self, reason: str) -> None:
        """SIGTERM the group now and SIGKILL it after the grace period."""
        if self.status["reason"] is None:
            self.status["reason"] = reason
        self.signal_group(signal.SIGTERM)
        if self._escalation is None:
            self._escalation = threading.Timer(self.grace, self.signal_group, (signal.SIGKILL,))
            self._escalation.daemon = True
            self._escalation.start()

    def _on_signal(self, signum: int, _frame: object) -> None:
        if signum == signal.SIGINT:
            self.status["reason"] = self.status["reason"] or "interrupted"
            self.signal_group(signal.SIGINT)
        else:
            self.terminate(signal.Signals(signum).name.lower())

    # ---- running -------------------------------------------------------------------

    def _watch_parent(self) -> None:
        parent = int(self.spec["parent_pid"])
        start = self.spec.get("parent_start")
        while self.proc is not None and self.proc.returncode is None:
            if not alive(parent, start):
                self.terminate("parent-gone")
                return
            time.sleep(POLL_SECONDS)

    def _tee(self, log) -> None:
        assert self.proc and self.proc.stdout
        window_ok = True
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace")
            log.write(line)
            log.flush()
            parse_usage(self.spec.get("provider", ""), line, self.usage)
            if window_ok:
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                except (OSError, ValueError):
                    window_ok = False           # the window is gone; keep logging

    def _reap(self) -> int:
        """Wait for the leader without reaping it, kill what it left in its group, reap it.

        The unreaped leader keeps its pgid reserved, so the group kill can never reach
        an unrelated process that reused the id."""
        assert self.proc
        os.waitid(os.P_PID, self.proc.pid, os.WEXITED | os.WNOWAIT)
        with self._lock:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._reaped = True
            return self.proc.wait()

    def run(self) -> int:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, self._on_signal)
        env = child_env(self.spec.get("provider"), os.environ, self.spec.get("env") or {})
        env.update(_stored_key_env(self.spec.get("provider")))
        log_path = Path(self.spec["log"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            try:
                self.proc = subprocess.Popen(self.spec["cmd"], cwd=self.spec["cwd"], env=env,
                                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as error:
                log.write(f"[jbi.shim] cannot start {self.spec['cmd'][0]}: {error}\n")
                self.status.update(started=now(), ended=now(), exit_code=127, reason="spawn-failed")
                write_json(self.status_path, self.status)
                return 127
            self.status.update(pid=self.proc.pid, pgid=self.proc.pid, start=process_start(self.proc.pid),
                               started=now())
            write_json(self.status_path, self.status)
            if self.status["reason"] in ("sigterm", "sighup"):
                self.terminate(self.status["reason"])   # a signal arrived before the start
            threading.Thread(target=self._watch_parent, daemon=True).start()
            reader = threading.Thread(target=self._tee, args=(log,), daemon=True)
            reader.start()
            code = self._reap()
            reader.join(timeout=2)
        if self._escalation:
            self._escalation.cancel()
        self.status.update(ended=now(), exit_code=code, usage=self.usage.as_dict())
        write_json(self.status_path, self.status)
        return code if code >= 0 else 128 - code


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python3 -m jbi.shim <spec.json>", file=sys.stderr)
        return 2
    return Shim(json.loads(Path(argv[0]).read_text())).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
