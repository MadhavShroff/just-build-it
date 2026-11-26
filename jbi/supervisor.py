"""The execution runtime: private tmux sessions, internal leases, direct CI, and stop modes.

`Supervisor` implements `jbi.contracts.Runtime`. Every agent runs through the shim in
its own private tmux window. The scheduler admits tasks when dependencies are merged.

A dispatch grants a lease before an agent execution. A verified CI merge ends the task.
A refusal preserves the lease and uncommitted work.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import agents, briefs, ci_adapter as ci_mod, guards, models, providers, rigor, taskkey, waves, worktrees as wt
from .contracts import Hooks
from .lanes import MergeJob
from .shim import Groups, alive, process_start, run_command
from .store import Changes, Store, now
from .tracking import Tracking

ACCEPTANCE_TIMEOUT = 900.0
# A provider that refuses a model as unknown ("model not recognized",
# "invalid_model") never serves that model again. Its lane cools for the whole run
# and the same stage re-runs on the next lane without an attempt penalty. The
# reroute bound keeps a run whose every lane is retired from spinning: past it, the
# failure counts like any other and the task escalates to a request.
PERMANENT_MODEL_TOKENS = ("model not recognized", "invalid_model")
PERMANENT_REROUTE_LIMIT = 6


def _permanent_model_error(detail: str) -> bool:
    """True when a dispatch failure text says the provider does not know the model."""
    lower = str(detail).lower()
    return any(token in lower for token in PERMANENT_MODEL_TOKENS)


TRANSIENT_PROVIDER_TOKENS = (
    "http 429",
    "rate_limit_error",
    "http 529",
    "overloaded_error",
    "resource_exhausted",
)


def _transient_provider_error(detail: str) -> bool:
    """True when a dispatch failed on provider rate-limiting or overload rather than task code."""
    lower = str(detail).lower()
    return any(token in lower for token in TRANSIENT_PROVIDER_TOKENS)
STANDDOWN_WAIT = 20.0
TICK = 1.0
CLOSE_WAIT = 10.0                 # a window close is verified within this time, not assumed
STAGE_STATUS = {"implement": "running", "merge": "queued"}
WINDOW_NAME = re.compile(r"^[a-z0-9-]+-wave\d+-task\d+-[a-z-]+$")    # REQUIREMENT 1.2


def _bounded_id(prefix: str, *parts: str) -> str:
    """Preserve readable short ids and bound longer unique task identities."""
    raw = "-".join((prefix, *parts))
    if len(raw) <= 64:
        return raw
    return prefix + "-" + hashlib.blake2s(raw.encode(), digest_size=16).hexdigest()


@dataclass(frozen=True)
class Window:
    """One tmux window: the name it shows and the id that stays unique.

    REQUIREMENT 1.2 fixes the name. The id is what a close targets, because two
    steps of one task can share a name when they share a provider.
    """
    name: str
    wid: str | None


class Tmux:
    """A private tmux server. Never touches the default server."""

    def __init__(self, socket: str, cwd: Path, parent_pid: int | None = None):
        self.socket, self.cwd = socket, cwd
        self.parent_pid = parent_pid or os.getpid()
        self.available = shutil.which("tmux") is not None
        self._started = False
        self._start_lock = threading.Lock()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = {**agents.runtime_env(), "PYTHONPATH": str(agents.PACKAGE_ROOT)}
        return subprocess.run(["tmux", "-L", self.socket, "-f", "/dev/null", *args], env=env, cwd=self.cwd,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def open(self, name: str, argv: list[str]) -> "Window | None":
        """Open a detached window running `argv`. Returns its name and id, or None without tmux.

        The four reviews of one task call this at the same time (founder rule
        2026-11-16), so the first-window setup is guarded: two threads that both
        saw `_started` false would both run `new-session`, and the second fails.
        Two concurrent steps can carry the same name, because the name holds the
        provider and not the stage; the WINDOW ID is what stays unique.
        """
        if not self.available:
            return None
        with self._start_lock:
            if not self._started:
                # The control window keeps the server open while the CLI lives, then ends the
                # server, so a CLI that dies without cleanup still leaves no tmux server behind.
                watch = (f"while kill -0 {self.parent_pid} 2>/dev/null; do sleep 1; done; "
                         f"tmux -L {self.socket} kill-server; rm -f {self._socket_path()}")
                self._run("new-session", "-d", "-s", "jbi", "-n", "control", "-x", "200", "-y", "50",
                          "--", "sh", "-c", watch)
                self._started = True
        result = self._run("new-window", "-d", "-t", "jbi:", "-n", name, "-P", "-F", "#{window_id}",
                           "--", *argv)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return Window(name, result.stdout.strip())

    def window(self, name: str, argv: list[str]) -> str | None:
        """Open a window and return only its name, for a caller that names no window of its own."""
        opened = self.open(name, argv)
        return opened.name if opened else None

    def _socket_path(self) -> Path:
        return Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / self.socket

    def _windows(self) -> dict[str, str]:
        """The live windows as `{window id: window name}`. Empty without tmux."""
        if not self.available:
            return {}
        result = self._run("list-windows", "-t", "jbi:", "-F", "#{window_id} #{window_name}")
        if result.returncode != 0:
            return {}
        found: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                found[parts[0].strip()] = parts[1].strip()
        return found

    def close(self, window: "Window") -> bool:
        """Close `window` and PROVE it is gone (REQUIREMENT 1.5). True means gone.

        A request to close is not a close: the window is asked to close, then its id
        is read back from the server. A window that survives the wait is killed by
        id and read back again. The answer is what is true, never what was asked.
        """
        if not self.available or window.wid is None:
            return True
        for _attempt in (1, 2):
            if window.wid not in self._windows():
                return True
            self._run("kill-window", "-t", window.wid)
            if self._await_gone(window.wid, CLOSE_WAIT):
                return True
        return window.wid not in self._windows()

    def _await_gone(self, wid: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if wid not in self._windows():
                return True
            time.sleep(0.05)
        return wid not in self._windows()

    def kill_server(self) -> None:
        if self.available:
            self._run("kill-server")
            self._socket_path().unlink(missing_ok=True)      # kill-server leaves the socket file behind
        self._started = False


@dataclass
class Session:
    sid: str
    task: str
    role: str
    status_path: Path
    window: str | None
    window_id: str | None = None
    direct: subprocess.Popen | None = None
    status: dict = field(default_factory=dict)
    finished: bool = False

    def read(self) -> dict:
        try:
            self.status = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            pass
        return self.status

    def done(self) -> bool:
        # Reclaim removes the status file (REQUIREMENT 1.6), so an ended step must
        # remember that it ended; a missing file is not a running step.
        return self.finished or self.read().get("ended") is not None


class Supervisor:
    def __init__(self, store: Store, hooks: Hooks, run_dir: Path, repo: Path | None = None, *,
                 parent_pid: int | None = None, use_tmux: bool = True,
                 ci_service: str = "ci-conductor", ci_adapter: ci_mod.CIAdapter | None = None,
                 tracking: Tracking | None = None):
        """`repo` pins the repository for tests. A real run reads `state.run.target`."""
        self.store, self.hooks = store, hooks
        self.run_dir, self._repo = Path(run_dir), Path(repo) if repo else None
        self.parent_pid = parent_pid or os.getpid()
        self.tracking = tracking or Tracking(self.run_dir / "fleet.db")
        self.ci_service = ci_service
        self.ci_adapter = ci_adapter or ci_mod.CIAdapter()
        self.active_dispatch: dict[str, int] = {}
        run_id = store.read("/run/id")
        # The CI service learns this run's identity on every service call, so its
        # completion notices route to this run (and never to the caller's pane).
        self.ci_adapter.env = {
            "ILLM_SLOT": str(store.read("/config/ci_notice_slot") or f"jbi-{run_id or 'run'}"),
            "JBI_RUN_ID": str(run_id or ""),
        }
        notice_url = str(store.read("/config/ci_notice_url") or "").strip()
        if notice_url:
            self.ci_adapter.env["JBI_CI_NOTICE_URL"] = notice_url
        self.tmux = Tmux(f"jbi-{run_id}", self.run_dir, self.parent_pid)
        self.tmux.available = self.tmux.available and use_tmux
        self.groups = Groups()
        self.sessions: dict[str, Session] = {}
        self.recorded_pids: set[int] = set()
        self.identities: dict[str, dict[int, str]] = {}   # sid -> {pid: start time} at spawn
        self.branch: str = wt.MAIN
        # --base: what every task branch cuts from. --branch: where verified work lands.
        # Both are set once at start and never reassigned. They may differ; the landing
        # branch alone advances (fast_forward_main), the base stays pinned.
        self.base_branch: str = wt.MAIN
        self._lock = threading.RLock()
        self.cooldowns = models.ProviderCooldowns()
        raw_pool = store.read("/config/provider_pool") or []
        self.provider_pool = models.ProviderPool(
            (item.get("provider"), item.get("model"))
            for item in raw_pool if isinstance(item, dict) and item.get("provider")
        ) if raw_pool else None
        self._task_cache = taskkey.WorkCache(self.run_dir, str(run_id or "run"))
        self._wake = threading.Event()
        self._stopping: str | None = None
        self._running: dict[str, threading.Thread] = {}
        self._merge_done: dict[str, threading.Event] = {}
        self._merge_ok: dict[str, bool] = {}
        self._merge_notes: dict[str, list[str]] = {}
        self._waves_done: set[int] = set()
        self._all_merged = False
        self._scheduler: threading.Thread | None = None
        self._handovers: dict[str, ci_mod.HandoverResult] = {}
        self._handover_lock = threading.Lock()
        self.ci_poll_interval = 1.0
        self._ci_lock = threading.Lock()
        self.session_dispatches: dict[str, int] = {}

    @property
    def repo(self) -> Path:
        return self._repo or Path(self.store.read("/run/target"))

    # ---- sessions --------------------------------------------------------------------

    def _lane_window(self, name: str, log: Path) -> str | None:
        return self.tmux.window(name, ["tail", "-n", "+1", "-F", str(log)])

    # REQUIREMENT 1.2 and 1.3: the name is
    # `<dagName>-wave<n>-task<n>-<provider>`. The wave is the task's compiled wave
    # index and the task number is its ordinal in that wave; both count from 1.

    def _restore_handovers(self) -> None:
        """Recover queued work whose PR and exact head still match the task."""
        if not hasattr(self.tracking, "pending_ci"):
            return
        for tid, task in (self.store.read("/plan") or {}).get("tasks", {}).items():
            records = self.tracking.pending_ci(tid)
            if not records or task.get("commit") is None:
                continue
            matching = [r for r in records if r["head_sha"] == task["commit"]]
            if not matching:
                continue
            record = matching[-1]
            reply = record["service_reply"]
            bound = reply.get("bound") if isinstance(reply, dict) else None
            if not bound or bound.get("task") != tid or bound.get("head_sha") != record["head_sha"]:
                raise RuntimeError(f"task {tid} has no bound handover")
            if int(bound.get("pr_number", 0)) != record["pr_number"]:
                raise RuntimeError(f"task {tid} has a PR mismatch")
            if not isinstance(reply.get("job"), str) or not reply["job"].strip():
                raise RuntimeError(f"task {tid} has no direct CI job id")
            self._handovers[tid] = ci_mod.HandoverResult(
                task=tid, branch=task["branch"], pr_number=record["pr_number"],
                head_sha=record["head_sha"], reply=reply, base_commit=record["base_commit"])

    @staticmethod
    def _slug(text: str, fallback: str = "dag") -> str:
        words = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
        return words or fallback

    def dag_name(self) -> str:
        """The plan's name, lowercased with hyphens. `dag` when the plan names none."""
        name = self.store.read("/plan/prd/name") or self.store.read("/run/name") or "dag"
        return self._slug(name)

    def placement(self, task: str) -> tuple[int, int]:
        """(wave, task ordinal within that wave), both 1-based. (1, 1) for no plan task."""
        plan = self.store.read("/plan") or {}
        task_doc = (plan.get("tasks") or {}).get(task) or {}
        waves_of_plan = plan.get("waves") or []
        wave = int(task_doc.get("wave") or 1)
        group = waves_of_plan[wave - 1] if 0 < wave <= len(waves_of_plan) else []
        return wave, (list(group).index(task) + 1 if task in group else 1)

    def window_name(self, task: str, provider: str) -> str:
        """The window name for one step of `task` on `provider` (REQUIREMENT 1.2)."""
        wave, ordinal = self.placement(task)
        return f"{self.dag_name()}-wave{wave}-task{ordinal}-{self._slug(provider, 'none')}"

    def _task_lease(self, task_id: str, cwd: Path | None = None) -> dict:
        """Register the task before agent execution. Return its one live lease."""
        with self._lock:
            task = self.store.read(f"/plan/tasks/{task_id}") or {}
            worktree = cwd or self.run_dir / "worktrees" / task_id
            owns = list(task.get("owns") or [])
            if task_id == "bootstrap":
                owns = ["*"]
            existing = self.tracking.get_task(task_id)
            base = (existing or {}).get("base_commit")
            if existing is None:
                base = wt.head(self.repo, self.store.read("/bootstrap/base_branch") or self.base_branch)
            if base is None:
                raise ValueError(f"task {task_id} needs a base commit")
            branch = f"jbi/{task_id}"
            self.tracking.ensure_task(task_id=task_id, title=str(task.get("title") or task_id),
                                      goal=str(task.get("goal") or ""), owns=owns, branch=branch,
                                      worktree=worktree, base_commit=base, node=self.dag_name())
            for lease in self.tracking.fleet()["leases"]:
                if lease["task_id"] == task_id and lease["released_at"] is None:
                    return lease
            current_task = self.tracking.get_task(task_id)
            if current_task is not None and current_task["state"] not in ("created", "abandoned", "merged"):
                self.tracking.mark_task_state(task_id, "abandoned")
            session_id = _bounded_id("s", self.dag_name(), task_id)
            grant = self.tracking.dispatch(task_id, session_id=session_id, owns=owns,
                                           branch=branch, worktree=worktree)
            return grant["lease"]

    def _stage_dispatch(self, sid: str, task_id: str, cwd: Path, model: str,
                        brief: Path | None = None, attempt: int = 0, provider: str = "") -> dict:
        """Renew a dispatch. Only a prior dispatch in this same task supplies history."""
        with self._lock:
            lease = self._task_lease(task_id, cwd)
            stage_key = re.sub(r"-\d+$", "", sid)
            # The history key names the provider: a cross-provider failover starts a fresh
            # transcript, because one provider's content blocks never replay into another's.
            history_key = f"h-{lease['session_id']}-{stage_key}-{provider}"
            if len(history_key) > 64:
                digest = hashlib.sha256(
                    f"{lease['session_id']}\0{stage_key}\0{provider}".encode()).hexdigest()
                history_key = f"h-{digest[:32]}-{lease['task_id'][-12:]}-{stage_key[-12:]}"
            self.tracking.start_history_session(history_key, node=self.dag_name())
            is_implement = stage_key == "implement" or stage_key.endswith("-implement")
            history = self.tracking.session_history(history_key) if (attempt > 0 and is_implement) else []
            digest = hashlib.sha256(brief.read_bytes()).hexdigest() if brief else None
            continuation = bool(history)
            dispatch = self.tracking.renew_dispatch(task_id, model=model, brief_digest=digest,
                                                    continuation=continuation)
            self.active_dispatch[sid] = dispatch["dispatch_id"]
            return {**dispatch, "continuation": continuation,
                    "history": history, "history_session_id": history_key,
                    "session_id": lease["session_id"]}

    def _store_lite_spec(self, task_id: str, role: str, cwd: Path, model: str, attempt: int,
                         brief: Path, receipt: Path, base: Path, timeout_sec: float, *,
                         provider: str = "") -> Path:
        dispatch = self._stage_dispatch(f"s-{task_id}-{role}-{attempt + 1}", task_id, cwd, model, brief,
                                        attempt=attempt, provider=provider)
        task = self.store.read(f"/plan/tasks/{task_id}") or {}
        lease = self._task_lease(task_id, cwd)
        raw_max_turns = int(rigor.task_timeouts(task)["max_turns"])
        max_turns = raw_max_turns if role == "implement" else min(raw_max_turns, 16)
        eff_timeout = float(timeout_sec or 1800) if role == "implement" else min(float(timeout_sec or 1080), 1080.0)
        spec = {"tracking": str(self.tracking.path), "workdir": str(cwd), "model": model,
                "task_id": task_id, "role": role, "owns": lease["owns"], "session_id": dispatch["session_id"],
                "history_session_id": dispatch["history_session_id"],
                "dispatch_id": dispatch["dispatch_id"], "continuation": dispatch["continuation"],
                "receipt": str(receipt), "checkpoint": str(base / "checkpoint.json"),
                "timeout_sec": eff_timeout, "max_turns": max_turns,
                "effort": rigor.effort_for(role, task=task, attempts=attempt), "brief": brief.read_text(encoding="utf-8"),
                "repo_context": json.dumps(self.store.read("/plan/prd") or {}, sort_keys=True),
                "frozen_contracts": json.dumps(task.get("contracts") or [], sort_keys=True),
                "usage_path": str(base / "usage.json")}
        if provider:
            spec["provider"] = provider
        base.mkdir(parents=True, exist_ok=True)
        path = base / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def run_session(self, task: str, role: str, cmd: list[str], cwd: Path, provider: str,
                    model: str, attempt: int, *, timeout_sec: float = 0.0,
                    idle_timeout_sec: float = 0.0) -> Session:
        """Start one agent under the shim, wait until it ends, then reclaim its window.

        REQUIREMENT 1.1 to 1.6: the step owns a window of its own, named by
        `window_name`. The window is closed and its process tree and temp files are
        reclaimed before this call returns; the close is verified, not requested.
        Only the stage receipt survives.
        """
        sid = f"s-{task}-{role}-{attempt + 1}"
        base = self.run_dir / "sessions" / sid
        base.parent.mkdir(parents=True, exist_ok=True)
        spec = {"cmd": cmd, "cwd": str(cwd), "provider": provider, "env": agents.extra_env(provider),
                "parent_pid": self.parent_pid, "parent_start": process_start(self.parent_pid),
                "status": f"{base}.status.json", "log": f"{base}.log"}
        lite_usage_path = base / "usage.json"
        if provider != "simulator":
            if cmd[:3] != [sys.executable, "-m", "claude_mux_lite"] or len(cmd) != 4:
                raise ValueError("a neutral agent needs its claude_mux_lite spec path")
            # The shim maps stored credentials to its inherited environment. None enters this spec.
            spec["env"] = {"PYTHONPATH": str(agents.PACKAGE_ROOT)}
        if timeout_sec > 0:
            spec["timeout_sec"] = float(timeout_sec)
        if idle_timeout_sec > 0:
            spec["idle_timeout_sec"] = float(idle_timeout_sec)
        Path(f"{base}.spec.json").write_text(json.dumps(spec))
        argv = [sys.executable, "-m", "jbi.shim", f"{base}.spec.json"]
        opened = self.tmux.open(self.window_name(task, provider), argv)
        window = opened.name if opened else None
        session = Session(sid, task, role, Path(spec["status"]), window,
                          opened.wid if opened else None)
        if window is None:
            session.direct = subprocess.Popen(argv, cwd=self.run_dir, start_new_session=True,
                                              env={**agents.runtime_env(), **agents.extra_env("simulator")},
                                              stdin=subprocess.DEVNULL)
        with self._lock:
            self.sessions[sid] = session
        self.store.commit("session.status", Changes().set("sessions", sid, {
            "task": task, "role": role, "provider": provider, "model": model,
            "window": window, "window_id": session.window_id,
            "status": "running", "started": now(),
            "note": None if window else "tmux is missing; the shim runs as a direct child"}),
            {"session": sid, "task": task})
        ident: dict[int, str] = {}
        last_beat = time.monotonic()
        try:
            while not session.done():
                self._record(session)
                if provider != "simulator" and (time.monotonic() - last_beat) >= 15.0:
                    last_beat = time.monotonic()
                    self.store.commit("telemetry.sample",
                                      Changes().set("sessions", sid, "heartbeat", now()),
                                      {"session": sid, "task": task, "heartbeat": True})
                time.sleep(0.1)
            self._record(session)
            if lite_usage_path.is_file():
                try:
                    session.status.update(json.loads(lite_usage_path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass
            log_path = Path(f"{base}.log")
            if log_path.is_file():
                try:
                    from . import providers as _providers
                    raw_log = log_path.read_text(encoding="utf-8", errors="replace").strip()
                    if raw_log:
                        session.status["stderr_excerpt"] = _providers.redact(raw_log[-2000:])
                except OSError:
                    pass
        finally:
            with self._lock:
                ident = self.identities.pop(sid, {})        # an ended session leaves the kill set
            session.finished = True
            try:
                self._end_session(session)
            except Exception:
                pass
            try:
                self._record_lite_usage(sid, session)
            except Exception:
                pass
            try:
                self._sync_fleet_session(session)
            except Exception:
                pass
            self._reclaim_window(session)
            self._stop_tree(ident)
            if session.direct is not None:
                try:
                    session.direct.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    session.direct.kill()
                    session.direct.wait(timeout=5)
            self._remove_session_files(base)
            if provider != "simulator" and base.parent.name == "sessions" and base.is_dir():
                shutil.rmtree(base, ignore_errors=True)
        return session

    def _reclaim_window(self, session: Session) -> None:
        """Close the step's window and prove it is gone (REQUIREMENT 1.5)."""
        if session.window_id is None:
            return
        if not self.tmux.close(Window(session.window or "", session.window_id)):
            self.store.log("session", f"{session.task}/{session.role}: window {session.window_id} "
                                      f"did not close within {CLOSE_WAIT:.0f}s", task=session.task)

    def _stop_tree(self, ident: dict[int, str]) -> None:
        """Stop every process recorded for the step, group first (REQUIREMENT 1.5)."""
        for pid, start in ident.items():
            self._verified_signal(pid, start, signal.SIGTERM, group=True)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and any(alive(pid, start) for pid, start in ident.items()):
            time.sleep(0.05)
        for pid, start in ident.items():
            self._verified_signal(pid, start, signal.SIGKILL, group=True)
            self._verified_signal(pid, start, signal.SIGKILL, group=False)

    def _record_lite_usage(self, sid: str, session: Session | None = None) -> None:
        base = self.run_dir / "sessions" / sid
        usage_path = base / "usage.json"
        did = self.active_dispatch.pop(sid, None)
        if not usage_path.is_file():
            if did is not None:
                code = (session.status.get("exit_code") if session else None)
                self.tracking.finish_dispatch(did, verdict="passed" if code == 0 else "failed")
            return
        try:
            usage = json.loads(usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if did is not None:
                self.tracking.finish_dispatch(did, verdict="failed")
            return
        if did is None:
            return
        self.tracking.record_cost(did, input_tokens=usage.get("input_tokens"),
                                  output_tokens=usage.get("output_tokens"),
                                  cache_read=usage.get("cache_read"), cache_write=usage.get("cache_write"))
        phase = "passed" if usage.get("exit_code") == 0 else "failed"
        self.tracking.finish_dispatch(did, verdict=phase)
        target = session or self.sessions.get(sid)
        if target:
            self.store.commit("session.status", Changes().set("sessions", sid, "usage", usage),
                              {"session": sid, "task": target.task})

    def _sync_fleet_session(self, session: Session) -> None:
        """Update the task's fleet.db session row so finished/failed agents never stay 'busy'."""
        lease = next((row for row in self.tracking.fleet()["leases"]
                      if row["task_id"] == session.task and row["released_at"] is None), None)
        if lease is None:
            return
        code = session.status.get("exit_code")
        target_status = "idle" if code == 0 else "failed"
        self.tracking.mark_session_status(lease["session_id"], target_status)

    def _session_backed(self, sid: str) -> bool:
        """True only when `sid` maps to a live, non-finished session owned by this supervisor."""
        with self._lock:
            sess = self.sessions.get(sid)
            ident = dict(self.identities.get(sid, {}))
        if sess is None or sess.finished:
            return False
        if sess.read().get("ended") is not None:
            return False
        if sess.direct is not None and sess.direct.poll() is not None:
            return False
        if ident and not any(alive(pid, start) for pid, start in ident.items()):
            return False
        return True

    def reconcile_sessions(self) -> list[str]:
        """Mark any store session stuck as 'running' with no live backing as 'lost'."""
        recorded = self.store.read("/sessions") or {}
        lost: list[str] = []
        changes = Changes()
        for sid, entry in recorded.items():
            if not isinstance(entry, dict) or entry.get("status") != "running":
                continue
            if not self._session_backed(sid):
                changes.replace("sessions", sid, "status", "lost")
                lost.append(sid)
        if changes:
            self.store.commit("session.status", changes, {"reconciled": lost})
        return lost

    @staticmethod
    def _remove_session_files(base: Path) -> None:
        """Drop the step's scratch files (REQUIREMENT 1.6). The receipt is not here: it lives in `receipts/`."""
        for suffix in (".spec.json", ".status.json", ".log"):
            Path(f"{base}{suffix}").unlink(missing_ok=True)
        for file_name in ("spec.json", "checkpoint.json"):
            (base / file_name).unlink(missing_ok=True)

    def _record(self, session: Session) -> None:
        ident = self.identities.setdefault(session.sid, {})
        for key, start_key in (("pid", "start"), ("shim_pid", "shim_start")):
            pid = session.status.get(key)
            if pid and pid not in ident:
                start = session.status.get(start_key) or process_start(pid)
                if start:
                    ident[pid] = start
                    self.recorded_pids.add(pid)

    @staticmethod
    def _verified_signal(pid: int, start: str, sig: int, group: bool) -> None:
        """Signal `pid` (or its group) only when it still is the process recorded at spawn."""
        if not pid or pid in (os.getpid(), os.getpgrp()) or process_start(pid) != start:
            return
        try:
            (os.killpg if group else os.kill)(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _end_session(self, session: Session) -> None:
        status, usage = session.status, session.status.get("usage") or {}
        changes = (Changes().replace("sessions", session.sid, "status",
                                     "ended" if status.get("exit_code") == 0 else "failed")
                   .set("sessions", session.sid, "exit_code", status.get("exit_code")))
        totals = self.store.read("/telemetry")
        for key in ("tokens_in", "tokens_out"):
            if usage.get(key) is not None:
                changes.replace("telemetry", key, (totals.get(key) or 0) + usage[key])
        if usage.get("cost_usd") is not None:
            changes.replace("telemetry", "cost_usd", (totals.get("cost_usd") or 0.0) + usage["cost_usd"])
        changes.replace("telemetry", "samples", totals.get("samples", 0) + 1)
        self.store.commit("telemetry.sample", changes, {"session": session.sid, "task": session.task})

    # ---- bootstrap -------------------------------------------------------------------

    def start_bootstrap(self) -> None:
        threading.Thread(target=self._bootstrap_safe, name="jbi-bootstrap", daemon=True).start()

    def retry_bootstrap(self) -> None:
        """Run bootstrap again in the same target, after a failure."""
        self.store.commit("bootstrap.status", Changes().replace("bootstrap", "status", "running")
                          .replace("bootstrap", "detail", "retrying"))
        self.start_bootstrap()

    def _bootstrap_safe(self) -> None:
        try:
            self._bootstrap()
        except Exception as error:              # never leave bootstrap "running"
            detail = f"{type(error).__name__}: {error}"
            try:
                self.store.commit("bootstrap.status", Changes().replace("bootstrap", "status", "failed")
                                  .replace("bootstrap", "detail", detail[:2000]))
            finally:
                try:
                    self.hooks.bootstrap_finished(False, detail, None)
                except Exception:
                    pass

    def _bootstrap(self) -> None:
        from . import bootstrap
        state = self.store.snapshot()[1]
        current = wt.init_repo(self.repo)
        # A named --base or --branch was stored at start. Bootstrap runs on whatever
        # branch the repository is on, but it must never overwrite that stored choice
        # with the repository's current branch.
        self.branch = self.store.read("/bootstrap/branch") or current
        self.base_branch = self.store.read("/bootstrap/base_branch") or self.branch
        if wt.missing_branch(self.repo, self.base_branch):
            raise wt.GitError(f"base branch {self.base_branch!r} does not exist in {self.repo}; "
                              "every task branch would cut from nothing")
        if wt.missing_branch(self.repo, self.branch) and wt.ensure_branch(self.repo, self.branch,
                                                                          self.base_branch):
            self.store.log("bootstrap", f"created branch {self.branch} from base {self.base_branch}",
                           etype="bootstrap.status")
        self.store.commit("bootstrap.status", Changes()
                          .set("bootstrap", "branch", self.branch)
                          .set("bootstrap", "base_branch", self.base_branch))
        task = {"id": "bootstrap", "title": "Bootstrap", "goal": "configure the target repository",
                "owns": ["*"], "acceptance": "build and lint exit 0"}
        base = wt.head(self.repo, self.branch)
        if base is None:
            wt.git(self.repo, "commit", "--allow-empty", "-m", "Initialize the repository base")
        self._task_lease("bootstrap", self.repo)
        if wt.git(self.repo, "symbolic-ref", "-q", "--short", "HEAD", check=False) != "jbi/bootstrap":
            if wt.head(self.repo, "jbi/bootstrap") is None:
                # Cut jbi/bootstrap at the named base, never at HEAD: the lease
                # records base_branch as the base the bootstrap work sits on.
                wt.git(self.repo, "checkout", "-q", "-b", "jbi/bootstrap", self.base_branch)
            else:
                wt.git(self.repo, "checkout", "-q", "jbi/bootstrap")
        brief, receipt = briefs.write(self.run_dir, task=task, role="bootstrap", stage="bootstrap", attempt=0)
        brief.write_text(bootstrap.render_brief(state, receipt), encoding="utf-8")
        try:
            choice = models.select(state, "implement")
            job = agents.AgentJob(choice.provider, choice.model, "bootstrap", "bootstrap", self.repo, brief, receipt)
            if choice.provider != "simulator":
                path = self._store_lite_spec("bootstrap", "bootstrap", self.repo, choice.model, 0, brief, receipt,
                                             self.run_dir / "sessions" / "s-bootstrap-bootstrap-1", 1800,
                                             provider=choice.provider)
                job = agents.AgentJob(**{**job.__dict__, "spec_path": path})
            self.run_session("bootstrap", "bootstrap", agents.command(job), self.repo, choice.provider,
                             choice.model, 0)
            ok, detail = bootstrap.check(self.repo, parent_pid=self.parent_pid, groups=self.groups)
            sha = wt.commit_all(self.repo, "Bootstrap the repository") or wt.head(self.repo)
            self.store.commit("bootstrap.status", Changes().replace("bootstrap", "detail", detail)
                              .replace("bootstrap", "commit", sha))
        finally:
            lease = next((row for row in self.tracking.fleet()["leases"]
                          if row["task_id"] == "bootstrap" and row["released_at"] is None), None)
            if lease is not None:
                head = wt.head(self.repo, "jbi/bootstrap")
                if head:
                    self.tracking.record_commit("bootstrap", head)
                self.tracking.release("bootstrap", reason="abandon", abandon=True, repository=self.repo)
        self.hooks.bootstrap_finished(ok, detail, sha)

    # ---- the scheduler ---------------------------------------------------------------

    def _refresh_base_from_origin(self) -> None:
        """Fast-forward the local base from origin before any worktree is cut.

        A stale base cuts every task branch from yesterday's commit. A failed fetch
        keeps the current base: an offline host still starts, and a rewritten origin
        never moves this host's branch."""
        before = wt.head(self.repo, self.base_branch)
        fetched = wt.fetch_branch(self.repo, self.base_branch, update_local=True)
        if fetched is None:
            self.store.log("run", f"could not refresh base {self.base_branch} from origin; "
                                  "the run starts from the local base")
        elif fetched != before:
            self.store.log("run", f"base {self.base_branch} fast-forwarded to {fetched[:10]}")
        elif before:
            self.store.log("run", f"origin holds no fast-forward for {self.base_branch}; "
                                  "the run starts from the local base")

    def _preflight_models(self) -> None:
        """Probe every provider the run's named models depend on before dispatch (H1).

        A provider whose probe fails loses its account now, so the first task never
        burns an attempt discovering a dead lane. A cascade, pin, or preference model
        the provider no longer lists cools for the whole run: a retired model never
        starts an agent."""
        state = self.store.snapshot()[1]
        wanted: dict[str, set[str]] = {}
        for entry in (state.get("config") or {}).get("provider_cascade") or []:
            if isinstance(entry, dict) and entry.get("provider") and entry.get("model"):
                wanted.setdefault(str(entry["provider"]).strip().lower(), set()).add(str(entry["model"]))
        for task in ((state.get("plan") or {}).get("tasks") or {}).values():
            override = task.get("override") or {}
            pin = override.get("pin")
            if isinstance(pin, dict) and pin.get("provider") and pin.get("model"):
                wanted.setdefault(str(pin["provider"]).strip().lower(), set()).add(str(pin["model"]))
            for want in override.get("prefer") or []:
                if isinstance(want, dict) and want.get("provider") and want.get("model"):
                    wanted.setdefault(str(want["provider"]).strip().lower(), set()).add(str(want["model"]))
        for provider, named in wanted.items():
            if provider not in providers.catalog()["providers"]:
                continue
            result = providers.probe(provider)
            if result.status in ("failed", "missing"):
                account = self.store.read(f"/accounts/{provider}") or providers.blank_account(provider)
                account.update(status=result.status, detail=result.detail)
                self.store.commit("account.status", Changes().set("accounts", provider, account),
                                  {"provider": provider})
                self.store.log("account", f"preflight: {provider} probe failed, the account is "
                                          f"disabled until a probe passes: {result.detail}")
                continue
            live = set(result.models or ())
            for model in sorted(named):
                if live and model not in live:
                    lane = f"{provider}/{model}"
                    self.cooldowns.mark(lane, math.inf)
                    self.store.log("cascade", f"preflight: {lane} is not in the provider's live "
                                              "model list; the lane cools for the whole run")

    def start_execution(self) -> None:
        self.branch = self.store.read("/bootstrap/branch") or self.branch
        self.base_branch = self.store.read("/bootstrap/base_branch") or self.branch
        if not self.store.read("/config/dry_run") and (self.repo / ".git").exists():
            self._preflight_models()
            self._refresh_base_from_origin()
            from . import resume as resume_mod
            runs_root = Path(os.environ.get("JBI_HOME", Path.home() / ".just-build-it")) / "runs"
            reclaimed = resume_mod.reclaim_start_orphans(self.repo, current_run_dir=self.run_dir,
                                                         runs_root=runs_root)
            if reclaimed.worktrees_removed or reclaimed.branches_removed:
                self.store.log(
                    "run",
                    f"Reclaimed {len(reclaimed.worktrees_removed)} orphan worktree(s) and "
                    f"{len(reclaimed.branches_removed)} jbi/* branch(es) before dispatch",
                )
        self._restore_handovers()
        self._scheduler = threading.Thread(target=self._schedule, name="jbi-scheduler", daemon=True)
        self._scheduler.start()

    def wake(self) -> None:
        self._wake.set()

    def set_branch(self, branch: str) -> None:
        self.branch = branch

    def _schedule(self) -> None:
        seq = self.store.seq
        while self._stopping not in ("quit", "kill", "standdown"):
            try:
                self.reconcile_sessions()
                self._admit()
            except Exception as error:
                try:
                    self.store.log("scheduler", f"scheduler tick error: {type(error).__name__}: {error}")
                except Exception:
                    print(f"[jbi.supervisor] scheduler tick error: {type(error).__name__}: {error}",
                          file=sys.stderr, flush=True)
            seq = self.store.wait(seq, TICK)
            self._wake.clear()

    def _admit(self) -> None:
        if self._stopping:
            return
        with self._lock:
            plan, config = self.store.read("/plan"), self.store.read("/config")
            state = self.store.snapshot()[1]
            admit_err = wt.host_admission_check(
                max_load_per_cpu=float(config.get("max_load_per_cpu") or 0.0),
                min_free_mem_mb=int(config.get("min_free_mem_mb") or 0),
            )
            if admit_err:
                return
            running = [t for t, th in self._running.items() if th.is_alive()]
            for tid in waves.prioritize_ready(plan, config=config):
                if len(running) >= config["max_sessions"]:
                    break
                if tid in running or waves.owns_conflict(plan, tid, running):
                    continue
                # A task that waits for a pinned model is offered again on every
                # pump. Skip it here while the pin still blocks, so a wait costs no
                # thread and no write. The moment the provider is usable the task
                # is admitted and starts.
                if plan["tasks"][tid]["status"] == "waiting-model" and models.blocked(state, plan["tasks"][tid]):
                    continue
                thread = threading.Thread(target=self._run_task, args=(tid,), name=f"jbi-{tid}", daemon=True)
                self._running[tid] = thread
                running.append(tid)
                thread.start()

    # ---- one task --------------------------------------------------------------------

    def _task_update(self, tid: str, etype: str = "task.status", **values: object) -> None:
        changes = Changes()
        for key, value in values.items():
            changes.set("plan", "tasks", tid, key, value)
        self.store.commit(etype, changes, {"task": tid})
        state = values.get("status")
        if state not in ("skipped", "waiting-model"):
            target = {"request": "blocked", "ready": "running"}.get(state, state)
            states = {r["id"]: r for r in self.tracking.fleet()["tasks"]}
            if tid in states and target in ("running", "verifying", "queued", "merging", "failed", "paused", "blocked", "abandoned"):
                if not (states[tid]["state"] == "merged" and target != "merged"):
                    if states[tid]["state"] == "abandoned" and target == "paused":
                        return
                    self.tracking.mark_task_state(tid, target)

    def _run_task(self, tid: str) -> None:
        try:
            self._run_task_body(tid)
        except Exception as error:              # a failed task is marked, never re-admitted in a loop
            note = f"{type(error).__name__}: {error}"
            try:
                self._task_update(tid, status="failed", findings=[note[:2000]])
                self.store.log("task", f"{tid} failed: {note[:300]}", task=tid)
            except Exception:
                pass

    def _stage_provider(self, tid: str, stage: str, attempt: int) -> str | None:
        """The provider that ran one stage, read from its session record.

        The stage record names the provider so a reader can see which one ran each
        stage. The session record already holds it, so the stage tuple that
        `_run_stage` returns keeps its shape.
        """
        return self.store.read(f"/sessions/s-{tid}-{stage}-{attempt + 1}/provider")

    def _run_task_body(self, tid: str) -> None:
        task = self.store.read(f"/plan/tasks/{tid}")
        # A PINNED task waits for its model. FOUNDER ORDER 2026-11-17: a pin is a
        # hard rule, so a task that pins a model runs on that model or waits. It
        # never falls back to the cascade. The check sits before the worktree is
        # made and before the first stage runs, so a waiting task starts no agent
        # and leaves nothing behind. The task is not failed: the scheduler admits
        # it again on a later pump, once the provider is usable.
        reason = models.blocked(self.store.snapshot()[1], task)
        if reason:
            # Write only on a change. The scheduler offers a waiting task again on
            # every pump, and a write on each pump would wake the scheduler again
            # and spin. An unchanged wait is silent.
            if task.get("status") != "waiting-model" or task.get("waiting_reason") != reason:
                self._task_update(tid, status="waiting-model", waiting_reason=reason)
                self.store.log("task", f"{tid} waits: {reason}", task=tid)
            return
        base = wt.head(self.repo, self.base_branch)
        if base is None:
            raise wt.GitError(f"base branch {self.base_branch} has no commit")
        existing = self.tracking.get_task(tid)
        if existing is not None:
            if existing.get("state") == "merged":
                merges = [m for m in self.tracking.fleet()["merges"] if m["task_id"] == tid]
                squash_sha = merges[-1]["squash_sha"] if merges else (existing.get("head_commit") or "")
                stages = dict(task.get("stages") or {})
                stages.setdefault("merge", {"status": "passed", "detail": f"merged {squash_sha[:12]}", "model": None})
                self.store.commit("task.merged", Changes()
                                  .set("plan", "tasks", tid, "status", "merged")
                                  .set("plan", "tasks", tid, "commit", squash_sha)
                                  .set("plan", "tasks", tid, "stages", stages)
                                  .set("plan", "tasks", tid, "findings", []),
                                  {"task": tid})
                self._progress()
                return
            base = existing["base_commit"]
        worktree = wt.restore_worktree(self.repo, tid, base, self.run_dir / "worktrees",
                                       expected_head=(existing or {}).get("head_commit"))
        wt.pin_git_metadata(worktree, self.repo)
        setup_cmds = self.store.read("/config/worktree_setup_commands") or ()
        wt.prepare_worktree(worktree, repo=self.repo, setup_commands=setup_cmds, cache_key=base)
        self._task_lease(tid, worktree)
        head_now = wt.head(worktree, "HEAD")
        if head_now:
            self.tracking.record_commit(tid, head_now)
        findings: list[str] = task.get("findings") or []
        stage_name: str | None = task.get("resume_after")
        permanent_reroutes = 0
        transient_reroutes = 0
        while not self._stopping or self._stopping == "finish":
            task = self.store.read(f"/plan/tasks/{tid}")
            if task.get("status") == "paused":     # stop after the current stage; keep progress
                self._task_update(tid, resume_after=stage_name)
                return
            level, profile = rigor.effective(task, self.store.read("/config"))
            stage = rigor.next_stage(level, stage_name)
            if stage is None and tid in self._handovers:
                if self._merge(tid, level, worktree):
                    self._reclaim_worktree(tid, worktree)
                    self._progress()
                return
            if stage is None:
                self._reclaim_worktree(tid, worktree)
                return
            key = self._taskkey(tid, stage, base, findings)
            hit = taskkey.consult(self._task_cache, key, stage, repo=self.repo, worktree=worktree)
            if hit is not None:
                ok, detail, model = True, hit.detail, hit.model
                if stage.name == "implement":
                    # Cache restoration changes the leased branch. Record its exact head.
                    self._task_update(tid, commit=hit.commit)
                    self.tracking.record_commit(tid, hit.commit)
                self.store.log("task", f"{tid}: {detail}", task=tid)
            else:
                ok, detail, model = self._run_stage(task, stage, level, worktree, findings)
            if self._stopping in ("quit", "kill", "standdown"):
                return
            if stage.name != rigor.REVIEW_CASCADE_STAGE:
                self.store.commit("task.stage", Changes()
                                  .set("plan", "tasks", tid, "stages", {**(task.get("stages") or {}),
                                       stage.name: {"status": "passed" if ok else "failed",
                                                    "detail": detail[:2000], "model": model,
                                                    "provider": self._stage_provider(
                                                        tid, stage.name, task.get("attempts", 0))}}),
                                  {"task": tid})
            if ok:
                if stage.role == "merge":
                    self._reclaim_worktree(tid, worktree)
                    self._progress()
                    return
                self._record_hit(tid, key, stage, ok, detail, model, worktree)
                # The four reviews run together inside the cascade stage, so the
                # loop must skip past the three DeepSeek stages it already ran.
                stage_name, findings = stage.name, []
                if stage.name == rigor.REVIEW_CASCADE_STAGE:
                    stage_name = rigor.REVIEW_DEEPSEEK_STAGES[-1]
                if self.store.read(f"/plan/tasks/{tid}/status") == "paused":
                    self._task_update(tid, resume_after=stage_name)
                    return
                continue
            if _permanent_model_error(detail) and permanent_reroutes < PERMANENT_REROUTE_LIMIT:
                # A retired model is a run defect, not task work: `_maybe_cool_lane`
                # already cooled the lane for the whole run, so the same stage re-runs
                # on the next lane. The attempt budget, the findings, and the resume
                # point stay untouched, because the failover must never read as task
                # failure. Past the bound the failure counts like any other, so a run
                # whose every lane is retired still escalates.
                permanent_reroutes += 1
                continue
            if _transient_provider_error(detail) and transient_reroutes < 3:
                # Rate limits (HTTP 429 / 529 / 503) already cooled the failing lane in
                # `_maybe_cool_lane`; fall through to the next cascade rung without
                # burning the task's attempt budget or halting on a request.
                transient_reroutes += 1
                continue
            attempts = task.get("attempts", 0) + 1
            raw_lines = detail.splitlines()
            findings = raw_lines if len(raw_lines) <= 20 else [*raw_lines[:10], *raw_lines[-10:]]
            history = [*(task.get("attempt_history") or []), {"stage": stage.name, "findings": findings}]
            self._task_update(tid, attempts=attempts, findings=findings, attempt_history=history[-20:],
                              resume_after=None)
            stage_name = None
            if attempts >= rigor.DECISION_AFTER:
                self._open_request(tid, attempts, findings)
                return
            if self.store.read(f"/plan/tasks/{tid}/status") == "paused":
                self._task_update(tid, resume_after=None)
                return

    def _reclaim_worktree(self, tid: str, worktree: Path) -> None:
        """Release a clean lease. Its detached worktree remains at the base.

        A dirty worktree cannot release. Its lease and uncommitted files remain.
        """
        dry_run = bool(self.store.read("/config/dry_run"))
        merged = not dry_run and self.store.read(f"/plan/tasks/{tid}/status") == "merged"
        if not merged:
            if (self.tracking.get_task(tid) or {}).get("state") != "abandoned":
                self.tracking.mark_task_state(tid, "paused")
        if not any(row["task_id"] == tid and row["released_at"] is None
                   for row in self.tracking.fleet()["leases"]):
            return
        try:
            self.tracking.release(tid, reason="merged" if merged else "abandon", abandon=not merged, repository=self.repo)
        except Exception as error:
            self.store.log("lease", f"{tid}: cannot release lease: {error}", task=tid)
            self._task_update(tid, status="paused" if not merged else "merged",
                              lease_state="held", release_error=str(error))

    def _taskkey(self, tid: str, stage: rigor.Stage, base: str,
                 findings: list[str]) -> str | None:
        """The key of one stage of `tid` (REQUIREMENT-task-keys 2.1).

        The pool and the cooldowns ride along, so the key's model input is the same
        choice the dispatch will make: a cooled lane hashes to its own key instead of
        reading a hit recorded by a different lane."""
        tasks = (self.store.read("/plan") or {}).get("tasks") or {}
        return self._task_cache.key_for(self.repo, tasks, tid, stage, base=base,
                                        state=self.store.snapshot()[1], findings=findings,
                                        pool=self.provider_pool, cooldowns=self.cooldowns)

    def _record_hit(self, tid: str, key: str | None, stage: rigor.Stage, ok: bool,
                    detail: str, model: str | None, worktree: Path) -> None:
        """Record a PASSED stage under its key (4.1, 4.3). A gate role is never recorded (4.2)."""
        taskkey.remember(self._task_cache, key, stage, task=tid, worktree=worktree,
                         ok=ok, detail=detail, model=model)

    def _run_stage(self, task: dict, stage: rigor.Stage, level: str, worktree: Path,
                   findings: list[str]) -> tuple[bool, str, str | None]:
        tid = task["id"]
        if self.store.read(f"/plan/tasks/{tid}/status") != "paused":
            self._task_update(tid, "task.stage", stage=stage.name,
                              status=STAGE_STATUS.get(stage.role, "verifying"))
        else:
            self._task_update(tid, "task.stage", stage=stage.name)
        if stage.role == "acceptance":
            wt.purge_tsbuildinfo(worktree)
            self._task_lease(tid, worktree)
            acc_timeout = float(rigor.task_timeouts(task).get("acceptance_timeout_s") or ACCEPTANCE_TIMEOUT)
            sid = f"s-{tid}-acceptance-{task.get('attempts', 0) + 1}"
            self._stage_dispatch(sid, tid, worktree,
                                 "acceptance", attempt=task.get("attempts", 0))
            try:
                from claude_mux_lite.sandbox import acceptance_argv
                acc_argv = acceptance_argv(task["acceptance"], worktree)
                result = run_command(acc_argv, cwd=worktree, env=agents.child_env(None),
                                     timeout=acc_timeout, parent_pid=self.parent_pid, groups=self.groups)
            finally:
                wt.discard_changes(worktree)
                did = self.active_dispatch.pop(sid, None)
                if did is not None:
                    self.tracking.finish_dispatch(did, verdict="ended")
            code, out = result.returncode, result.output
            return code == 0, f"exit {'timeout' if code is None else code}\n{out[-2000:]}", None
        if stage.role == "merge":
            ok = self._merge(tid, level, worktree)
            notes = self._merge_notes.pop(tid, None)
            return ok, "merged" if ok else ("\n".join(notes) if notes else "merge failed"), None
        if stage.name == rigor.REVIEW_CASCADE_STAGE:
            return self._review_fanout(task, worktree, findings)
        return self._agent_stage(task, stage, worktree, findings)

    def _review_fanout(self, task: dict, worktree: Path,
                       findings: list[str]) -> tuple[bool, str, str | None]:
        """FOUNDER RULE 2026-11-16: run the four reviews at the same time.

        One review on the model the cascade names as active, and the named reviewers
        of `data/models.json` beside it. The reviewers are not coordinated with each
        other or with the first. Every review must pass; the findings of every
        failing review are returned together, so the implementer sees all the reads
        at once.

        A reviewer stage that resolved to a model an earlier reviewer already runs
        adds no independent read: four identical reads burn four times the quota for
        one opinion. The duplicate is skipped and its stage record says so, unless
        `data/models.json` names a distinct model for it.
        """
        stages = rigor.review_stages()
        state = self.store.snapshot()[1]
        attempt = task.get("attempts", 0)
        reviewers = models.reviewer_overrides()
        choices: dict[str, models.Choice] = {}
        for stage in stages:
            choice = models.select(state, stage.role, task=task, attempts=attempt,
                                   pool=self.provider_pool, cooldowns=self.cooldowns)
            override = reviewers.get(stage.name)
            if override is not None and override[0] in models.usable(state) \
                    and not self.cooldowns.cooling(f"{override[0]}/{override[1]}"):
                choice = models.Choice(override[0], override[1], choice.tier)
            choices[stage.name] = choice
        lanes: dict[tuple[str, str], str] = {}
        run_stages: list[rigor.Stage] = []
        duplicate_of: dict[str, str] = {}
        for stage in stages:
            mark = (choices[stage.name].provider, choices[stage.name].model)
            if mark in lanes:
                duplicate_of[stage.name] = lanes[mark]
                continue
            lanes[mark] = stage.name
            run_stages.append(stage)
        results: dict[str, tuple[bool, str, str | None]] = {}
        lock = threading.Lock()

        def one(stage: rigor.Stage) -> None:
            try:
                got = self._agent_stage(task, stage, worktree, findings, discard=False,
                                        forced_choice=choices[stage.name])
            except Exception as error:          # one review failing never hides the others
                got = (False, f"{type(error).__name__}: {error}", None)
            with lock:
                results[stage.name] = got

        threads = [threading.Thread(target=one, args=(s,), name=f"jbi-{task['id']}-{s.name}",
                                    daemon=True) for s in run_stages]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wt.discard_changes(worktree)
        ordered: list[tuple[bool, str, str | None]] = []
        for s in stages:
            if s.name in duplicate_of:
                ordered.append((True, f"skipped: the same reviewer model as {duplicate_of[s.name]} "
                                      "(name a distinct model in data/models.json)", None))
            else:
                ordered.append(results.get(s.name, (False, "review did not report", None)))
        if self._stopping not in ("quit", "kill", "standdown"):
            tid = task["id"]
            recorded = dict(self.store.read(f"/plan/tasks/{tid}/stages") or {})
            for s, (s_ok, s_detail, s_model) in zip(stages, ordered):
                recorded[s.name] = {
                    "status": "passed" if s_ok else "failed",
                    "detail": s_detail[:2000],
                    "model": s_model,
                    "provider": self._stage_provider(tid, s.name, task.get("attempts", 0)),
                }
            self.store.commit("task.stage", Changes().set("plan", "tasks", tid, "stages", recorded),
                              {"task": tid})
        passed = all(ok for ok, _detail, _model in ordered)
        detail = "\n".join(
            "%s: %s" % (s.name, d) for s, (ok, d, _m) in zip(stages, ordered) if not ok
        ) or "all four reviews passed"
        reviewer_models = ",".join(sorted({m for _ok, _d, m in ordered if m}))
        return passed, detail, reviewer_models or None

    def _agent_stage(self, task: dict, stage: rigor.Stage, worktree: Path,
                     findings: list[str], *, discard: bool = True,
                     forced_choice: models.Choice | None = None) -> tuple[bool, str, str | None]:
        tid, attempt = task["id"], task.get("attempts", 0)
        state = self.store.snapshot()[1]
        choice = forced_choice or models.select(state, stage.role, task=task, attempts=attempt,
                                                pool=self.provider_pool, cooldowns=self.cooldowns)
        shared = briefs.discover_shared_helpers(worktree) if stage.role == "implement" else None
        limits = rigor.task_timeouts(task)
        ckpt_path = self.run_dir / "checkpoints" / f"{tid}.json"
        ckpt_note: str | None = None
        if stage.role == "implement" and ckpt_path.is_file():
            try:
                data = json.loads(ckpt_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    ckpt_note = f"Resuming after {data.get('reason', 'previous attempt')}."
            except (OSError, ValueError):
                pass
        brief, receipt = briefs.write(self.run_dir, task=task, role=stage.role, stage=stage.name,
                                      attempt=attempt, findings=findings if stage.role == "implement" else None,
                                      shared_helpers=shared, checkpoint_note=ckpt_note)
        job = agents.AgentJob(choice.provider, choice.model, tid, stage.role, worktree, brief, receipt,
                              attempt, tuple(task.get("owns") or ()))
        if choice.provider != "simulator":
            self._task_lease(tid, worktree)
            lite_spec = self._store_lite_spec(
                tid, stage.name, worktree, choice.model, attempt, brief, receipt,
                self.run_dir / "sessions" / f"s-{tid}-{stage.name}-{attempt + 1}", limits["agent_timeout_s"],
                provider=choice.provider)
            job = agents.AgentJob(**{**job.__dict__, "spec_path": lite_spec})
        limits = rigor.task_timeouts(task)
        use_watchdog = choice.provider != "simulator" or bool(state.get("config", {}).get("enforce_timeouts"))
        if use_watchdog:
            session = self.run_session(
                tid, stage.name, agents.command(job), worktree, choice.provider, choice.model, attempt,
                timeout_sec=float(limits["agent_timeout_s"]),
                idle_timeout_sec=float(limits["idle_timeout_s"]),
            )
        else:
            session = self.run_session(tid, stage.name, agents.command(job), worktree, choice.provider,
                                       choice.model, attempt)
        if self._stopping in ("quit", "kill", "standdown"):
            return False, f"stopped by {self._stopping}", choice.model
        reason = session.status.get("reason")
        if reason in ("timeout", "idle-timeout") and stage.role == "implement":
            try:
                ckpt_path.parent.mkdir(parents=True, exist_ok=True)
                ckpt_path.write_text(json.dumps({"task": tid, "attempt": attempt + 1, "reason": reason,
                                                 "provider": choice.provider, "model": choice.model}), encoding="utf-8")
            except OSError:
                pass
        result = briefs.parse_receipt(receipt)
        if stage.role == "implement":
            owns = task.get("owns") or []
            check_syntax = choice.provider != "simulator" or bool(state.get("config", {}).get("enforce_syntax_guards"))
            if check_syntax:
                guard_errors = guards.validate_worktree_edits(worktree, owns)
            elif owns:
                unowned = [p for p in wt.dirty_paths(worktree) if not wt.path_owned(p, owns)]
                guard_errors = [f"modified unowned path {', '.join(unowned)}"] if unowned else []
            else:
                guard_errors = []
            if guard_errors:
                if any(e.startswith("modified unowned path") for e in guard_errors):
                    wt.discard_changes(worktree)
                else:
                    draft_sha = wt.commit_all(worktree, f"{tid}: implement draft attempt {attempt + 1}") or wt.head(worktree, "HEAD")
                    if draft_sha:
                        self._task_update(tid, commit=draft_sha)
                        self.tracking.record_commit(tid, draft_sha)
                return False, "; ".join(guard_errors), choice.model
            sha = wt.commit_all(worktree, f"{tid}: implement attempt {attempt + 1}")
            sha = sha or wt.head(worktree, "HEAD")
            self._task_update(tid, commit=sha, receipt=str(receipt))
            if sha:
                self.tracking.record_commit(tid, sha)
            ckpt_path.unlink(missing_ok=True)
        elif discard:
            wt.discard_changes(worktree)        # verifiers and reviewers never edit code
        if result is None:
            excerpt = str(session.status.get("final_text") or session.status.get("stderr_excerpt") or "").strip()
            code = session.status.get("exit_code")
            if excerpt:
                suffix = f" (exit {code}, {reason})" if reason else f" (exit {code})"
                detail = f"{excerpt[:1500]}{suffix}"
            elif reason:
                detail = f"no valid receipt (exit {code}, {reason})"
            else:
                detail = f"no valid receipt (exit {code}, agent produced no stderr)"
            self._maybe_cool_lane(tid, choice, session, detail)
            return False, detail, choice.model
        return result.passed, "\n".join(result.findings), choice.model

    def _maybe_cool_lane(self, tid: str, choice: models.Choice, session: Session, detail: str) -> None:
        """Cool a provider/model lane when dispatch failed before productive work or hit a provider limit.

        A provider that no longer knows the model ("model not recognized",
        "invalid_model") lost the lane for the whole run, not for two minutes: a
        120-second cooldown just re-burns an attempt every time it thaws."""
        if choice.provider == "simulator":
            return
        code = session.status.get("exit_code")
        permanent = _permanent_model_error(detail)
        if code in (None, 0) and not permanent:
            return
        lower = detail.lower()
        # A before_run refusal ("workdir is not clean", a live lease) is the
        # environment's fault, never the provider's: cooling its lane would flip
        # the whole run onto the next rung for nothing.
        if "before_run refused" in lower or "prompt too large" in lower:
            return
        lane_key = f"{choice.provider}/{choice.model}"
        if permanent:
            self.cooldowns.mark(lane_key, math.inf)
            self.store.log("cascade", f"{tid}: {lane_key} is retired by its provider; "
                                      "the lane cools for the whole run", task=tid)
            return
        provider_error = any(tok in lower for tok in (
            "429", "529", "401", "403", "502", "503", "504",
            "rate limit", "rate_limit", "quota", "overloaded", "capacity",
            "authentication_error", "transport failure", "connection refused",
            "resource_exhausted", "credential broker failed",
            "retries exhausted",
        ))
        zero_turn_error = session.status.get("turns") == 0
        if provider_error or zero_turn_error:
            self.cooldowns.mark(lane_key, 120.0)
            self.store.log("cascade", f"{tid}: {lane_key} cooled after dispatch error", task=tid)

    # ---- merging ---------------------------------------------------------------------

    def _merge(self, tid: str, level: str, worktree: Path) -> bool:
        dry_run = bool(self.store.read("/config/dry_run"))
        if dry_run:
            task = self.store.read(f"/plan/tasks/{tid}") or {}
            head = task.get("commit") or wt.head(worktree, "HEAD") or ""
            self.store.log("ci", f"DRY RUN: {tid} would submit {head[:10]} through ci-conductor. "
                                 "Nothing was pushed or merged.", task=tid)
            self._task_update(tid, status="merged", commit=head, dry_run=True)
            return True
        done = threading.Event()
        self._merge_done[tid] = done
        self._merge_ok.pop(tid, None)
        target_branch = self.store.read("/config/epic_branch") if level == "wave-epic" else None
        job = MergeJob(tid, f"jbi/{tid}", level, worktree, target_branch=target_branch)
        handover: ci_mod.HandoverResult | None = None
        try:
            # The handover lock covers creation, lookup, and the dict write only. The
            # CI wait below never holds it: a poll runs 20-60 minutes, and a lock held
            # across that wait serializes every other lane's merge behind it and
            # deadlocks `ci_notice`, which needs the same lock to deliver its answer.
            with self._handover_lock:
                task = self.store.read(f"/plan/tasks/{tid}")
                expected_head = wt.head(worktree, "HEAD") or task.get("commit")
                handover = self._handovers.get(tid)
                if handover is not None and expected_head and handover.head_sha != expected_head:
                    handover = None
                    self._handovers.pop(tid, None)
                if expected_head and task.get("commit") != expected_head:
                    self._task_update(tid, commit=expected_head)
                    task = self.store.read(f"/plan/tasks/{tid}")
                if handover is None:
                    original = self.tracking.get_task(tid) or {}
                    existing = [row for row in self.tracking.pending_ci(tid)
                                if row["head_sha"] == task.get("commit")]
                    if original.get("state") == "merging" and len(existing) == 1:
                        row = existing[0]
                        handover = ci_mod.HandoverResult(tid, f"jbi/{tid}", row["pr_number"], row["head_sha"],
                                                         row["service_reply"], row["base_commit"])
                    elif original.get("head_commit") == task.get("commit"):
                        records = [row for row in self.tracking.fleet()["ci_submissions"]
                                   if row["task_id"] == tid and row["state"] == "green"
                                   and row["head_sha"] == task.get("commit")]
                        if len(records) == 1:
                            row = records[0]
                            handover = ci_mod.HandoverResult(tid, f"jbi/{tid}", row["pr_number"], row["head_sha"],
                                                             row["service_reply"], row["base_commit"])
                    if handover is not None:
                        self._handovers[tid] = handover
                if handover is None:
                    handover = self.ci_adapter.handover(self.repo, task, base=target_branch or self.branch,
                                                       tracking=self.tracking)
                    self._handovers[tid] = handover
                self._task_update(tid, status="merging", pr_number=handover.pr_number,
                                  commit=handover.head_sha)
            while not done.is_set() and self._stopping not in ("quit", "kill", "standdown"):
                if self.ci_adapter.pending(self.repo, handover):
                    time.sleep(self.ci_poll_interval)
                    continue
                # The wait stays parallel, but the confirm and the landing
                # serialize: two lanes confirming sibling squashes against one
                # moving base would diverge, and the service sequences trains.
                # Both locks are held for subprocess reads and one fast-forward.
                with self._handover_lock, self._ci_lock:
                    if done.is_set():        # a notice answered while we polled
                        break
                    evidence = self.ci_adapter.confirm_merge(self.repo, handover, tracking=self.tracking)
                    if not self._finish_verified_merge(job, handover, evidence["squash_sha"]):
                        break
                time.sleep(self.ci_poll_interval)
        except (ci_mod.CIAdapterError, OSError, ValueError, KeyError) as error:
            if self.store.read(f"/plan/tasks/{tid}/status") == "merged":
                return True
            bound = self._handovers.pop(tid, None)
            if bound is not None and (self.tracking.get_task(tid) or {}).get("state") != "merged":
                self._record_ci_failure(bound, str(error))
            self._merge_failed(job, [str(error)])
        return self._merge_ok.pop(tid, False)

    def _record_ci_failure(self, handover: ci_mod.HandoverResult, detail: str) -> None:
        """Close the bound queued record without changing a verified terminal record."""
        tid = handover.task
        rows = self.tracking.pending_ci(tid)
        wanted = next((row for row in rows if row["head_sha"] == handover.head_sha
                      and row["pr_number"] == handover.pr_number), None)
        reply = {"type": "data", "text": detail, "job": handover.reply.get("job"),
                 "bound": handover.reply.get("bound")}
        self.tracking.record_ci(tid, pr_number=handover.pr_number, head_sha=handover.head_sha,
                                base_commit=handover.base_commit, state="failed", service_reply=reply)
        if wanted is not None:
            self._handovers.pop(tid, None)

    def _finish_verified_merge(self, job: MergeJob, handover: ci_mod.HandoverResult, squash: str) -> bool:
        """Release the exact branch before moving the local base to its CI squash."""
        tid = job.task
        result = self.tracking.get_task(tid)
        if self.store.read(f"/plan/tasks/{tid}/status") == "merged":
            self._merge_ok[tid] = True
            if tid in self._merge_done:
                self._merge_done[tid].set()
            return True
        if (result or {}).get("state") != "merged":
            raise ci_mod.CIAdapterError("verified task state changed before cleanup")
        live = any(row["task_id"] == tid and row["released_at"] is None
                   for row in self.tracking.fleet()["leases"])
        if live:
            try:
                self.tracking.release(tid, reason="merged", repository=self.repo)
            except Exception as error:
                self._handovers[tid] = handover
                self.store.log("lease", f"{tid}: verified CI cannot release its worktree: {error}", task=tid)
                self._merge_notes[tid] = [str(error)]
                self._task_update(tid, status="merging", lease_state="held", release_error=str(error))
                return False
        base = job.target_branch or self.branch
        current = wt.head(self.repo, base)
        if not current:
            raise ci_mod.CIAdapterError(f"base {base} has no commit")
        already_in_base = wt.is_ancestor(self.repo, squash, current)
        if not already_in_base:
            if live and not wt.is_ancestor(self.repo, handover.base_commit, current):
                raise ci_mod.CIAdapterError(f"base {base} diverged before task lease release")
            if live and current != handover.base_commit and not wt.is_ancestor(self.repo, current, squash):
                raise ci_mod.CIAdapterError(f"base {base} changed after CI verification; submit a fresh exact head")
            if current != squash and not wt.fast_forward_main(self.repo, squash, current, branch=base):
                raise ci_mod.CIAdapterError(f"base {base} changed before CI squash synchronization")
        self._handovers.pop(tid, None)
        self._merged(job, squash)
        return True

    def _merged(self, job: MergeJob, sha: str) -> None:
        if self._merge_ok.get(job.task) and self.store.read(f"/plan/tasks/{job.task}/commit") == sha:
            if job.task in self._merge_done:
                self._merge_done[job.task].set()
            return
        self._task_update(job.task, status="merged", commit=sha)
        patch_file = job.worktree / ".jbi" / "architecture.patch.json"
        try:
            patch = json.loads(patch_file.read_text())
        except (OSError, ValueError):
            patch = None
        self.hooks.task_merged(job.task, sha, patch)
        self._merge_ok[job.task] = True
        if job.task in self._merge_done:
            self._merge_done[job.task].set()
        self._progress()
        self.wake()

    def _merge_failed(self, job: MergeJob, notes: list[str]) -> None:
        self._merge_ok[job.task] = False
        self._merge_notes[job.task] = list(notes)
        self.store.log("lane", f"{job.task} failed in a lane: {notes[0] if notes else ''}", task=job.task)
        if job.task in self._merge_done:
            self._merge_done[job.task].set()

    def ci_notice(self, tid: str, notice: dict) -> None:
        self._merge_done.setdefault(tid, threading.Event())
        self._merge_ok.pop(tid, None)
        # A notice cannot bypass the current-base handover lock of a concurrent poll.
        with self._handover_lock, self._ci_lock:
            existing = self.tracking.get_task(tid)
            if self.store.read(f"/plan/tasks/{tid}/status") == "merged" or (existing and existing.get("state") == "merged"):
                if self.store.read(f"/plan/tasks/{tid}/status") != "merged":
                    merges = [m for m in self.tracking.fleet()["merges"] if m["task_id"] == tid]
                    squash_sha = merges[-1]["squash_sha"] if merges else (
                        notice.get("merge_sha") or (existing or {}).get("head_commit") or "")
                    stages = dict(self.store.read(f"/plan/tasks/{tid}/stages") or {})
                    stages.setdefault("merge", {"status": "passed", "detail": f"merged {squash_sha[:12]}", "model": None})
                    self.store.commit("task.merged", Changes()
                                      .set("plan", "tasks", tid, "status", "merged")
                                      .set("plan", "tasks", tid, "commit", squash_sha)
                                      .set("plan", "tasks", tid, "stages", stages)
                                      .set("plan", "tasks", tid, "findings", []),
                                      {"task": tid})
                    self._progress()
                self._merge_ok[tid] = True
                self._merge_done[tid].set()
                return
            job = MergeJob(tid, f"jbi/{tid}", self.store.read("/config/rigor") or "high",
                           self.run_dir / "worktrees" / tid)
            handover = self._handovers.get(tid)
            if handover is None and (notice.get("job") or notice.get("job_id")):
                wanted = str(notice.get("job") or notice.get("job_id"))
                rows = [row for row in self.tracking.fleet()["ci_submissions"]
                        if row["task_id"] == tid and row["service_reply"].get("job") == wanted]
                if rows:
                    row = rows[-1]
                    handover = ci_mod.HandoverResult(
                        task=tid, branch=f"jbi/{tid}", pr_number=row["pr_number"],
                        head_sha=row["head_sha"], reply=row["service_reply"], base_commit=row["base_commit"])
                    self._handovers[tid] = handover
            if handover is None and hasattr(self.tracking, "pending_ci"):
                rows = self.tracking.pending_ci(tid)
                if rows:
                    row = rows[-1]
                    handover = ci_mod.HandoverResult(
                        task=tid, branch=f"jbi/{tid}", pr_number=row["pr_number"],
                        head_sha=row["head_sha"], reply=row["service_reply"], base_commit=row["base_commit"])
                    self._handovers[tid] = handover
            if handover is None:
                self._merge_failed(job, ["No bound CI handover exists. Read the direct job record."])
                return
            try:
                evidence = self.ci_adapter.confirm_merge(
                    self.repo, handover,
                    job=str(notice.get("job_id") or notice.get("job") or "") or None,
                    tracking=self.tracking,
                )
            except ci_mod.CIAdapterError:
                self._merge_failed(job, ci_mod.format_failure_findings(notice))
            else:
                self._finish_verified_merge(job, handover, evidence["squash_sha"])

    def _block_dependents(self, skipped: str) -> None:
        plan = self.store.read("/plan")
        changes = Changes()
        for tid in plan.get("order", []):
            task = plan["tasks"][tid]
            if skipped in task.get("deps", []) and task["status"] in ("blocked", "ready"):
                changes.set("plan", "tasks", tid, "status", "blocked")
                changes.set("plan", "tasks", tid, "blocked_reason", f"dependency {skipped} was skipped")
        if changes:
            self.store.commit("task.status", changes, {"task": skipped})

    def _progress(self) -> None:
        plan = self.store.read("/plan")
        for number, wave in enumerate(plan["waves"], start=1):
            if number not in self._waves_done and all(plan["tasks"][t]["status"] == "merged" for t in wave):
                self._waves_done.add(number)
                self.hooks.wave_completed(number)
        # A task's status turns "merged" inside the lane callback, before the task
        # thread commits the merge stage record. Firing all_merged on status alone
        # lets a reader see a merged task with no merge stage. Require the record.
        if not self._all_merged and all(
                t["status"] == "merged" and "merge" in (t.get("stages") or {})
                for t in plan["tasks"].values()):
            self._all_merged = True
            self.hooks.all_merged()

    # ---- user requests and control ---------------------------------------------------

    def _open_request(self, tid: str, attempts: int, findings: list[str]) -> None:
        rid = f"r-{tid}-{attempts}"
        self.store.commit("request.opened", Changes().set("requests", rid, {
            "title": f"{tid} failed {attempts} times", "body": "\n".join(findings[:10]),
            "options": ["retry", "skip", "pause"], "status": "open", "owner": "runtime", "task": tid}),
            {"request": rid, "task": tid})
        self._task_update(tid, status="request")

    def answer(self, request_id: str, answer: str) -> None:
        tid = self.store.read(f"/requests/{request_id}/task")
        if tid:
            self.control(tid, {"skip": "skip", "pause": "pause", "resume": "resume"}.get(answer, "retry"))

    def update_task_owns(self, task_id: str, owns: list[str]) -> list[str]:
        from . import tracking as tracking_mod
        normalized = tracking_mod.normalize_owns(owns)
        if self.tracking.get_task(task_id) is not None:
            return self.tracking.update_task_owns(task_id, normalized)["owns"]
        return normalized

    def send_message(self, target: str, text: str, all_sessions: bool = False) -> None:
        for message in self.tracking.send(target, text, all_sessions=all_sessions):
            self.tracking.message_receipt(message["id"], "sent", "recorded for the agent")
            self.store.log("message", f"Message accepted for {message['target']}", target=message["target"])


    def control(self, task: str, action: str) -> None:
        if action == "pause":
            self._task_update(task, status="paused")
        elif action == "skip":
            self._task_update(task, status="skipped")
            self._block_dependents(task)
        elif action == "resume":
            # Continue from the stage after the last completed one, with the same attempts.
            self._task_update(task, status="ready")
        else:
            # Retry: back to implement. Attempts and attempt_history stay for escalation.
            self._task_update(task, status="ready", stage=None, resume_after=None)
        self.wake()

    # ---- stop modes ------------------------------------------------------------------

    def stop(self, mode: str) -> None:
        """Every mode returns at once, except "kill": its SIGKILLs finish before it returns."""
        self._stopping = mode
        self.wake()
        if mode == "kill":
            self._kill_now()
        threading.Thread(target=self._stop, args=(mode,), name=f"jbi-stop-{mode}", daemon=True).start()

    def _kill_now(self) -> None:
        """SIGKILL now, no grace: every live, verified session process and command group."""
        for session in self._live_sessions():
            self._record(session)
        with self._lock:
            idents = [dict(v) for v in self.identities.values()]
        for ident in idents:
            for pid, start in ident.items():
                self._verified_signal(pid, start, signal.SIGKILL, group=True)
                self._verified_signal(pid, start, signal.SIGKILL, group=False)
        self.groups.signal_all(signal.SIGKILL)

    def _live_sessions(self) -> list[Session]:
        with self._lock:
            return [s for s in self.sessions.values() if not s.done()]

    def _signal_sessions(self, sig: int, groups_too: bool) -> None:
        for session in self._live_sessions():
            self._record(session)
            with self._lock:
                ident = dict(self.identities.get(session.sid, {}))
            shim, pid = session.status.get("shim_pid"), session.status.get("pid")
            if shim in ident:
                self._verified_signal(shim, ident[shim], sig, group=False)
            if groups_too and pid in ident:
                self._verified_signal(pid, ident[pid], sig, group=True)
        self.groups.signal_all(sig)

    def _wait_sessions(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while self._live_sessions() and time.monotonic() < deadline:
            time.sleep(0.1)

    def _stop(self, mode: str) -> None:
        if mode == "finish":
            for thread in list(self._running.values()):
                thread.join()
        if mode == "standdown":
            self._signal_sessions(signal.SIGINT, groups_too=False)
            self._wait_sessions(STANDDOWN_WAIT)
            for tid in list(self._running):
                worktree = self.run_dir / "worktrees" / tid
                if (worktree / ".git").exists():
                    task = self.tracking.get_task(tid) or {}
                    unsafe = [rel for rel in wt.dirty_paths(worktree)
                              if not wt.path_owned(rel, task.get("owns") or [])]
                    if unsafe:
                        self.store.log("task", f"{tid}: unowned work preserved; stand-down commit refused: "
                                       f"{', '.join(unsafe)}", task=tid)
                        continue
                    try:
                        wt.commit_all(worktree, f"{tid}: work in progress (stand down)")
                    except wt.GitError as error:
                        self.store.log("task", f"{tid}: uncommitted work preserved: {error}", task=tid)
                if self.store.read(f"/plan/tasks/{tid}/status") == "merged":
                    continue
                self.tracking.mark_task_state(tid, "paused")
                head = wt.head(worktree, "HEAD")
                if head:
                    self.tracking.record_commit(tid, head)
                try:
                    self.tracking.release(tid, reason="abandon", abandon=True, repository=self.repo)
                except Exception as error:
                    self.store.log("lease", f"{tid}: uncommitted work and lease preserved: {error}", task=tid)
                self._task_update(tid, status="paused")
        if mode == "kill":
            self._kill_now()
        else:
            self._signal_sessions(signal.SIGTERM, groups_too=True)
            self._wait_sessions(10.0)
        self.tmux.kill_server()
        for session in list(self.sessions.values()):
            if session.direct:
                try:
                    session.direct.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        self.hooks.stopped(mode)

    def recorded(self) -> set[int]:
        """Every pid and pgid that the run recorded, for liveness checks."""
        pids = set(self.recorded_pids) | set(self.groups.seen)
        for session in self.sessions.values():
            pids.update(v for v in (session.read().get("pid"), session.status.get("shim_pid")) if v)
        return {p for p in pids if p}


__all__ = ["Supervisor", "Tmux", "alive"]
