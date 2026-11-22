"""The execution runtime: private tmux sessions, internal leases, direct CI, and stop modes.

`Supervisor` implements `jbi.contracts.Runtime`. Every agent runs through the shim in
its own private tmux window. The scheduler admits tasks when dependencies are merged.

A dispatch grants a lease before an agent execution. A verified CI merge ends the task.
A refusal preserves the lease and uncommitted work.
"""

from __future__ import annotations

import hashlib
import json
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

from . import agents, briefs, ci_adapter as ci_mod, providers, guards, models, rigor, taskkey, waves, worktrees as wt
from .contracts import Hooks
from .lanes import MergeJob
from claude_mux_lite.harness import DEFAULT_BASE_URL
from .shim import Groups, alive, process_start, run_command
from .store import Changes, Store, now
from .tracking import Tracking

ACCEPTANCE_TIMEOUT = 900.0
STANDDOWN_WAIT = 20.0
TICK = 1.0
CLOSE_WAIT = 10.0                 # a window close is verified within this time, not assumed
STAGE_STATUS = {"implement": "running", "merge": "queued"}
WINDOW_NAME = re.compile(r"^[a-z0-9-]+-wave\d+-task\d+-[a-z-]+$")    # REQUIREMENT 1.2


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
        self.tmux = Tmux(f"jbi-{run_id}", self.run_dir, self.parent_pid)
        self.tmux.available = self.tmux.available and use_tmux
        self.groups = Groups()
        self.sessions: dict[str, Session] = {}
        self.recorded_pids: set[int] = set()
        self.identities: dict[str, dict[int, str]] = {}   # sid -> {pid: start time} at spawn
        self.branch: str = wt.MAIN
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
            if len(records) != 1:
                raise RuntimeError(f"task {tid} has ambiguous unanswered CI submissions")
            record = records[0]
            reply = record["service_reply"]
            bound = reply.get("bound") if isinstance(reply, dict) else None
            if task["commit"] != record["head_sha"]:
                continue
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
        task = self.store.read(f"/plan/tasks/{task_id}") or {}
        worktree = cwd or self.run_dir / "worktrees" / task_id
        owns = list(task.get("owns") or [])
        if not owns and task_id == "bootstrap":
            owns = [".jbi", ".gitignore"]
        base = wt.head(self.repo, self.store.read("/bootstrap/branch") or self.branch)
        if base is None:
            raise ValueError(f"task {task_id} needs a base commit")
        branch = f"jbi/{task_id}"
        self.tracking.ensure_task(task_id=task_id, title=str(task.get("title") or task_id),
                                  goal=str(task.get("goal") or ""), owns=owns, branch=branch,
                                  worktree=worktree, base_commit=base, node=self.dag_name())
        for lease in self.tracking.fleet()["leases"]:
            if lease["task_id"] == task_id and lease["released_at"] is None:
                return lease
        grant = self.tracking.dispatch(task_id, session_id=f"s-{self.dag_name()}-{task_id}", owns=owns,
                                       branch=branch, worktree=worktree)
        return grant["lease"]

    def _stage_dispatch(self, sid: str, task_id: str, cwd: Path, model: str,
                        brief: Path | None = None, attempt: int = 0) -> dict:
        """Renew a dispatch. Only a prior dispatch in this same task supplies history."""
        lease = self._task_lease(task_id, cwd)
        history = self.tracking.session_history(lease["session_id"]) if attempt > 0 else []
        digest = hashlib.sha256(brief.read_bytes()).hexdigest() if brief else None
        continuation = bool(history)
        dispatch = self.tracking.renew_dispatch(task_id, model=model, brief_digest=digest,
                                                continuation=continuation)
        self.active_dispatch[sid] = dispatch["dispatch_id"]
        return {**dispatch, "continuation": continuation,
                "history": history,
                "session_id": lease["session_id"]}

    def _store_lite_spec(self, task_id: str, role: str, cwd: Path, model: str, attempt: int,
                         brief: Path, receipt: Path, base: Path, timeout_sec: float) -> Path:
        dispatch = self._stage_dispatch(f"s-{task_id}-{role}-{attempt + 1}", task_id, cwd, model, brief,
                                        attempt=attempt)
        task = self.store.read(f"/plan/tasks/{task_id}") or {}
        lease = self._task_lease(task_id, cwd)
        spec = {"tracking": str(self.tracking.path), "workdir": str(cwd), "model": model,
                "task_id": task_id, "owns": lease["owns"], "session_id": dispatch["session_id"],
                "dispatch_id": dispatch["dispatch_id"], "continuation": dispatch["continuation"],
                "receipt": str(receipt), "checkpoint": str(base / "checkpoint.json"),
                "timeout_sec": timeout_sec or 1800, "max_turns": rigor.task_timeouts(task)["max_turns"],
                "effort": rigor.effort_for(role, task=task, attempts=attempt), "brief": brief.read_text(encoding="utf-8"),
                "repo_context": json.dumps(self.store.read("/plan/prd") or {}, sort_keys=True),
                "frozen_contracts": json.dumps(task.get("contracts") or [], sort_keys=True),
                "usage_path": str(base / "usage.json")}
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
        lite_usage_path = base.parent / "usage.json"
        if provider != "simulator":
            if cmd[:3] != [sys.executable, "-m", "claude_mux_lite"] or len(cmd) != 4:
                raise ValueError("a neutral agent needs its claude_mux_lite spec path")
            transport = {name: os.environ[name] for name in agents.PROVIDER_ENV["claude-mux-lite"] if name in os.environ}
            for key, value in providers.child_key_env(provider).items():
                if key.endswith("_API_KEY") and "CLAUDE_MUX_API_KEY" not in transport:
                    transport["CLAUDE_MUX_API_KEY"] = value
            transport.setdefault("CLAUDE_MUX_BASE_URL", os.environ.get("JBI_OPENAI_BASE_URL", DEFAULT_BASE_URL))
            spec["env"] = {"PYTHONPATH": str(agents.PACKAGE_ROOT), **transport}
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
        while not session.done():
            self._record(session)
            time.sleep(0.1)
        self._record(session)
        with self._lock:
            ident = self.identities.pop(sid, {})            # an ended session leaves the kill set
        self._end_session(session)
        if lite_usage_path.is_file():
            session.status.update(json.loads(lite_usage_path.read_text(encoding="utf-8")))
        self._record_lite_usage(sid)
        session.finished = True
        self._reclaim_window(session)
        self._stop_tree(ident)
        self._remove_session_files(base)
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

    def _record_lite_usage(self, sid: str) -> None:
        base = self.run_dir / "sessions" / sid
        usage_path = base / "usage.json"
        if not usage_path.is_file():
            return
        usage = json.loads(usage_path.read_text(encoding="utf-8"))
        did = self.active_dispatch.pop(sid, None)
        if did is None:
            return
        self.tracking.record_cost(did, input_tokens=usage.get("input_tokens"),
                                  output_tokens=usage.get("output_tokens"),
                                  cache_read=usage.get("cache_read"), cache_write=usage.get("cache_write"))
        phase = "passed" if usage.get("exit_code") == 0 else "failed"
        self.tracking.finish_dispatch(did, verdict=phase)
        session = self.sessions.get(sid)
        if session:
            self.store.commit("session.status", Changes().set("sessions", sid, "usage", usage),
                              {"session": sid, "task": session.task})

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
        self.branch = wt.init_repo(self.repo)
        task = {"id": "bootstrap", "title": "Bootstrap", "goal": "configure the target repository",
                "owns": [".jbi", ".gitignore"], "acceptance": "build and lint exit 0"}
        self.store.commit("bootstrap.status", Changes().set("bootstrap", "branch", self.branch))
        base = wt.head(self.repo, self.branch)
        if base is None:
            wt.git(self.repo, "commit", "--allow-empty", "-m", "Initialize the repository base")
        self._task_lease("bootstrap", self.repo)
        brief, receipt = briefs.write(self.run_dir, task=task, role="bootstrap", stage="bootstrap", attempt=0)
        choice = models.select(state, "implement")
        job = agents.AgentJob(choice.provider, choice.model, "bootstrap", "bootstrap", self.repo, brief, receipt)
        if choice.provider != "simulator":
            path = self._store_lite_spec("bootstrap", "bootstrap", self.repo, choice.model, 0, brief, receipt,
                                         self.run_dir / "sessions" / "s-bootstrap-bootstrap-1", 1800)
            job = agents.AgentJob(**{**job.__dict__, "spec_path": path})
        self.run_session("bootstrap", "bootstrap", agents.command(job), self.repo, choice.provider,
                         choice.model, 0)
        ok, detail = bootstrap.check(self.repo, parent_pid=self.parent_pid, groups=self.groups)
        sha = wt.commit_all(self.repo, "Bootstrap the repository") or wt.head(self.repo)
        self.store.commit("bootstrap.status", Changes().replace("bootstrap", "detail", detail)
                          .replace("bootstrap", "commit", sha))
        self.hooks.bootstrap_finished(ok, detail, sha)

    # ---- the scheduler ---------------------------------------------------------------

    def start_execution(self) -> None:
        self.branch = self.store.read("/bootstrap/branch") or self.branch
        self._restore_handovers()
        self._scheduler = threading.Thread(target=self._schedule, name="jbi-scheduler", daemon=True)
        self._scheduler.start()

    def wake(self) -> None:
        self._wake.set()

    def _schedule(self) -> None:
        seq = self.store.seq
        while self._stopping not in ("quit", "kill", "standdown"):
            self._admit()
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
        base = wt.head(self.repo, self.branch)
        if base is None:
            raise wt.GitError(f"base branch {self.branch} has no commit")
        worktree = wt.create_worktree(self.repo, tid, base, self.run_dir / "worktrees")
        setup_cmds = self.store.read("/config/worktree_setup_commands") or ()
        wt.prepare_worktree(worktree, setup_commands=setup_cmds, cache_key=base)
        self._task_lease(tid, worktree)
        findings: list[str] = task.get("findings") or []
        stage_name: str | None = task.get("resume_after")
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
            attempts = task.get("attempts", 0) + 1
            findings = detail.splitlines()[:20]
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
            self.tracking.mark_task_state(tid, "paused")
        try:
            self.tracking.release(tid, reason="merged" if merged else "abandon", abandon=not merged)
        except Exception as error:
            self.store.log("lease", f"{tid}: cannot release lease: {error}", task=tid)
            self._task_update(tid, status="paused" if not merged else "failed",
                              lease_state="held", release_error=str(error))

    def _taskkey(self, tid: str, stage: rigor.Stage, base: str,
                 findings: list[str]) -> str | None:
        """The key of one stage of `tid` (REQUIREMENT-task-keys 2.1)."""
        tasks = (self.store.read("/plan") or {}).get("tasks") or {}
        return self._task_cache.key_for(self.repo, tasks, tid, stage, base=base,
                                        state=self.store.snapshot()[1], findings=findings)

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
            self._stage_dispatch(f"s-{tid}-acceptance-{task.get('attempts', 0) + 1}", tid, worktree,
                                 "acceptance", attempt=task.get("attempts", 0))
            acc_timeout = float(rigor.task_timeouts(task).get("acceptance_timeout_s") or ACCEPTANCE_TIMEOUT)
            result = run_command(task["acceptance"], cwd=worktree, env=agents.child_env(None),
                                 timeout=acc_timeout, parent_pid=self.parent_pid, groups=self.groups)
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

        One review on the model the cascade service names as active, and three
        independent DeepSeek agents. The three are not coordinated with each
        other or with the first. Every review must pass; the findings of every
        failing review are returned together, so the implementer sees all four
        reads at once.
        """
        stages = rigor.review_stages()
        results: dict[str, tuple[bool, str, str | None]] = {}
        lock = threading.Lock()

        def one(stage: rigor.Stage) -> None:
            try:
                got = self._agent_stage(task, stage, worktree, findings, discard=False)
            except Exception as error:          # one review failing never hides the others
                got = (False, f"{type(error).__name__}: {error}", None)
            with lock:
                results[stage.name] = got

        threads = [threading.Thread(target=one, args=(s,), name=f"jbi-{task['id']}-{s.name}",
                                    daemon=True) for s in stages]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wt.discard_changes(worktree)
        ordered = [results.get(s.name, (False, "review did not report", None)) for s in stages]
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
        models = ",".join(sorted({m for _ok, _d, m in ordered if m}))
        return passed, detail, models or None

    def _agent_stage(self, task: dict, stage: rigor.Stage, worktree: Path,
                     findings: list[str], *, discard: bool = True) -> tuple[bool, str, str | None]:
        tid, attempt = task["id"], task.get("attempts", 0)
        state = self.store.snapshot()[1]
        choice = models.select(state, stage.role, task=task, attempts=attempt,
                               pool=self.provider_pool, cooldowns=self.cooldowns)
        shared = briefs.discover_shared_helpers(worktree) if stage.role == "implement" else None
        limits = rigor.task_timeouts(task)
        ckpt_path = worktree / ".jbi_checkpoint.json"
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
                self.run_dir / "sessions" / f"s-{tid}-{stage.name}-{attempt + 1}", limits["agent_timeout_s"])
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
                wt.discard_changes(worktree)
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
            return False, f"no valid receipt (exit {session.status.get('exit_code')}, reason {reason or 'none'})", choice.model
        return result.passed, "\n".join(result.findings), choice.model

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
        target_branch = self.store.read("/config/epic_branch") if level == "wave-epic" else None
        job = MergeJob(tid, f"jbi/{tid}", level, worktree, target_branch=target_branch)
        try:
            task = self.store.read(f"/plan/tasks/{tid}")
            handover = self._handovers.get(tid)
            if handover is None:
                handover = self.ci_adapter.handover(self.repo, task, base=target_branch or self.branch,
                                                   tracking=self.tracking)
                self._handovers[tid] = handover
            self._task_update(tid, status="merging", pr_number=handover.pr_number, commit=handover.head_sha)
            while not done.is_set() and self._stopping not in ("quit", "kill", "standdown"):
                if self.ci_adapter.pending(self.repo, handover):
                    time.sleep(self.ci_poll_interval)
                    continue
                evidence = self.ci_adapter.confirm_merge(self.repo, handover, tracking=self.tracking)
                self._merged(job, evidence["squash_sha"])
                time.sleep(self.ci_poll_interval)
        except (ci_mod.CIAdapterError, OSError, ValueError, KeyError) as error:
            bound = self._handovers.pop(tid, None)
            if bound is not None:
                self.tracking.record_ci(tid, pr_number=bound.pr_number, head_sha=bound.head_sha,
                                       base_commit=bound.base_commit, state="failed",
                                       service_reply={"type": "data", "text": str(error), "job": bound.reply.get("job")})
            self._merge_failed(job, [str(error)])
        return self._merge_ok.pop(tid, False)

    def _merged(self, job: MergeJob, sha: str) -> None:
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
        with self._ci_lock:
            job = MergeJob(tid, f"jbi/{tid}", self.store.read("/config/rigor") or "high",
                           self.run_dir / "worktrees" / tid)
            handover = self._handovers.get(tid)
            if handover is None and hasattr(self.tracking, "pending_ci"):
                rows = self.tracking.pending_ci(tid)
                if len(rows) == 1:
                    row = rows[0]
                    self._handovers[tid] = handover = ci_mod.HandoverResult(
                        task=tid, branch=f"jbi/{tid}", pr_number=row["pr_number"],
                        head_sha=row["head_sha"], reply=row["service_reply"], base_commit=row["base_commit"])
            if handover is None:
                self._merge_failed(job, ["No bound CI handover exists. Read the direct job record."])
                return
            try:
                evidence = self.ci_adapter.confirm_merge(self.repo, handover, tracking=self.tracking)
            except ci_mod.CIAdapterError:
                self._merge_failed(job, ci_mod.format_failure_findings(notice))
            else:
                self._merged(job, evidence["squash_sha"])

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
            self.control(tid, {"skip": "skip", "pause": "pause"}.get(answer, "retry"))

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
                    self.tracking.release(tid, reason="abandon", abandon=True)
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
