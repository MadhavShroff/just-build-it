"""The execution runtime: tmux sessions, the scheduler, stage runs, CI lanes, stop modes.

`Supervisor` implements `jbi.contracts.Runtime`. Every agent runs under `jbi.shim` in
its own window of a private tmux server (`tmux -L jbi-<run-id>`). The scheduler admits
a task as soon as its own dependencies are merged, never by wave.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import agents, briefs, models, rigor, waves, worktrees as wt
from .contracts import Hooks
from .lanes import Lanes, MergeJob
from .shim import Groups, alive, process_start, run_command
from .store import Changes, Store, now

ACCEPTANCE_TIMEOUT = 900.0
STANDDOWN_WAIT = 20.0
TICK = 1.0
STAGE_STATUS = {"implement": "running", "merge": "queued"}


class Tmux:
    """A private tmux server. Never touches the default server."""

    def __init__(self, socket: str, cwd: Path, parent_pid: int | None = None):
        self.socket, self.cwd = socket, cwd
        self.parent_pid = parent_pid or os.getpid()
        self.available = shutil.which("tmux") is not None
        self._started = False

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = {**agents.runtime_env(), "PYTHONPATH": str(agents.PACKAGE_ROOT)}
        return subprocess.run(["tmux", "-L", self.socket, "-f", "/dev/null", *args], env=env, cwd=self.cwd,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def window(self, name: str, argv: list[str]) -> str | None:
        """Open a detached window running `argv`. Returns the window name, or None without tmux."""
        if not self.available:
            return None
        if not self._started:
            # The control window keeps the server open while the CLI lives, then ends the
            # server, so a CLI that dies without cleanup still leaves no tmux server behind.
            watch = (f"while kill -0 {self.parent_pid} 2>/dev/null; do sleep 1; done; "
                     f"tmux -L {self.socket} kill-server; rm -f {self._socket_path()}")
            self._run("new-session", "-d", "-s", "jbi", "-n", "control", "-x", "200", "-y", "50",
                      "--", "sh", "-c", watch)
            self._started = True
        result = self._run("new-window", "-d", "-t", "jbi:", "-n", name, "--", *argv)
        return name if result.returncode == 0 else None

    def _socket_path(self) -> Path:
        return Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / self.socket

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
    direct: subprocess.Popen | None = None
    status: dict = field(default_factory=dict)

    def read(self) -> dict:
        try:
            self.status = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            pass
        return self.status

    def done(self) -> bool:
        return self.read().get("ended") is not None


class Supervisor:
    def __init__(self, store: Store, hooks: Hooks, run_dir: Path, repo: Path | None = None, *,
                 parent_pid: int | None = None, use_tmux: bool = True):
        """`repo` pins the repository for tests. A real run leaves it None: the repository is
        always `state.run.target`, which the user can change until bootstrap starts."""
        self.store, self.hooks = store, hooks
        self.run_dir, self._repo = Path(run_dir), Path(repo) if repo else None
        self.parent_pid = parent_pid or os.getpid()
        run_id = store.read("/run/id")
        self.tmux = Tmux(f"jbi-{run_id}", self.run_dir, self.parent_pid)
        self.tmux.available = self.tmux.available and use_tmux
        self.groups = Groups()
        self.sessions: dict[str, Session] = {}
        self.recorded_pids: set[int] = set()
        self.identities: dict[str, dict[int, str]] = {}   # sid -> {pid: start time} at spawn
        self.branch: str = wt.MAIN
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping: str | None = None
        self._running: dict[str, threading.Thread] = {}
        self._merge_done: dict[str, threading.Event] = {}
        self._merge_ok: dict[str, bool] = {}
        self._waves_done: set[int] = set()
        self._all_merged = False
        self._scheduler: threading.Thread | None = None
        lanes = int(store.read("/config/lanes") or 1)
        self.lanes = Lanes(lanes, store=store, repo=lambda: self.repo, run_dir=self.run_dir, groups=self.groups,
                           on_merged=self._merged, on_failed=self._merge_failed, open_window=self._lane_window,
                           branch=lambda: self.branch, parent_pid=self.parent_pid)

    @property
    def repo(self) -> Path:
        return self._repo or Path(self.store.read("/run/target"))

    # ---- sessions --------------------------------------------------------------------

    def _lane_window(self, name: str, log: Path) -> str | None:
        return self.tmux.window(name, ["tail", "-n", "+1", "-F", str(log)])

    def run_session(self, task: str, role: str, cmd: list[str], cwd: Path, provider: str,
                    model: str, attempt: int) -> Session:
        """Start one agent under the shim and wait until it ends."""
        sid = f"s-{task}-{role}-{attempt + 1}"
        base = self.run_dir / "sessions" / sid
        base.parent.mkdir(parents=True, exist_ok=True)
        spec = {"cmd": cmd, "cwd": str(cwd), "provider": provider, "env": agents.extra_env(provider),
                "parent_pid": self.parent_pid, "parent_start": process_start(self.parent_pid),
                "status": f"{base}.status.json", "log": f"{base}.log"}
        Path(f"{base}.spec.json").write_text(json.dumps(spec))
        argv = [sys.executable, "-m", "jbi.shim", f"{base}.spec.json"]
        window = self.tmux.window(f"{task}:{role}", argv)
        session = Session(sid, task, role, Path(spec["status"]), window)
        if window is None:
            session.direct = subprocess.Popen(argv, cwd=self.run_dir, start_new_session=True,
                                              env={**agents.runtime_env(), **agents.extra_env("simulator")},
                                              stdin=subprocess.DEVNULL)
        with self._lock:
            self.sessions[sid] = session
        self.store.commit("session.status", Changes().set("sessions", sid, {
            "task": task, "role": role, "model": model, "window": window, "status": "running",
            "started": now(), "note": None if window else "tmux is missing; the shim runs as a direct child"}),
            {"session": sid, "task": task})
        while not session.done():
            self._record(session)
            time.sleep(0.1)
        self._record(session)
        with self._lock:
            self.identities.pop(sid, None)              # an ended session leaves the kill set
        self._end_session(session)
        return session

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
        self.store.commit("bootstrap.status", Changes().set("bootstrap", "branch", self.branch))
        task = {"id": "bootstrap", "title": "Bootstrap", "goal": bootstrap.brief(state),
                "owns": [], "acceptance": "build and lint exit 0"}
        brief, receipt = briefs.write(self.run_dir, task=task, role="bootstrap", stage="bootstrap", attempt=0)
        choice = models.select(state, "implement")
        job = agents.AgentJob(choice.provider, choice.model, "bootstrap", "bootstrap", self.repo, brief, receipt)
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
        self.lanes.start()
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
            running = [t for t, th in self._running.items() if th.is_alive()]
            for tid in waves.ready_tasks(plan):
                if len(running) >= config["max_sessions"]:
                    break
                if tid in running or waves.owns_conflict(plan, tid, running):
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

    def _run_task_body(self, tid: str) -> None:
        task = self.store.read(f"/plan/tasks/{tid}")
        base = wt.head(self.repo, self.branch)
        if base is None:
            raise wt.GitError(f"base branch {self.branch} has no commit")
        worktree = wt.create_worktree(self.repo, tid, base, self.run_dir / "worktrees")
        findings: list[str] = task.get("findings") or []
        stage_name: str | None = task.get("resume_after")
        while not self._stopping or self._stopping == "finish":
            task = self.store.read(f"/plan/tasks/{tid}")
            if task.get("status") == "paused":     # stop after the current stage; keep progress
                self._task_update(tid, resume_after=stage_name)
                return
            level, profile = rigor.effective(task, self.store.read("/config"))
            stage = rigor.next_stage(level, stage_name)
            if stage is None:
                return
            ok, detail, model = self._run_stage(task, stage, level, worktree, findings)
            if self._stopping in ("quit", "kill", "standdown"):
                return
            self.store.commit("task.stage", Changes()
                              .set("plan", "tasks", tid, "stages", {**(task.get("stages") or {}),
                                   stage.name: {"status": "passed" if ok else "failed",
                                                "detail": detail[:2000], "model": model}}), {"task": tid})
            if ok:
                if stage.role == "merge":
                    return
                stage_name, findings = stage.name, []
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
                self._open_decision(tid, attempts, findings)
                return
            if self.store.read(f"/plan/tasks/{tid}/status") == "paused":
                self._task_update(tid, resume_after=None)
                return

    def _run_stage(self, task: dict, stage: rigor.Stage, level: str, worktree: Path,
                   findings: list[str]) -> tuple[bool, str, str | None]:
        tid = task["id"]
        if self.store.read(f"/plan/tasks/{tid}/status") != "paused":
            self._task_update(tid, "task.stage", stage=stage.name,
                              status=STAGE_STATUS.get(stage.role, "verifying"))
        else:
            self._task_update(tid, "task.stage", stage=stage.name)
        if stage.role == "acceptance":
            result = run_command(task["acceptance"], cwd=worktree, env=agents.child_env(None),
                                 timeout=ACCEPTANCE_TIMEOUT, parent_pid=self.parent_pid, groups=self.groups)
            code, out = result.returncode, result.output
            return code == 0, f"exit {'timeout' if code is None else code}\n{out[-2000:]}", None
        if stage.role == "merge":
            return self._merge(tid, level, worktree), "merged", None
        return self._agent_stage(task, stage, worktree, findings)

    def _agent_stage(self, task: dict, stage: rigor.Stage, worktree: Path,
                     findings: list[str]) -> tuple[bool, str, str | None]:
        tid, attempt = task["id"], task.get("attempts", 0)
        state = self.store.snapshot()[1]
        choice = models.select(state, stage.role, task=task, attempts=attempt)
        brief, receipt = briefs.write(self.run_dir, task=task, role=stage.role, stage=stage.name,
                                      attempt=attempt, findings=findings if stage.role == "implement" else None)
        job = agents.AgentJob(choice.provider, choice.model, tid, stage.role, worktree, brief, receipt,
                              attempt, tuple(task.get("owns") or ()))
        session = self.run_session(tid, stage.name, agents.command(job), worktree, choice.provider,
                                   choice.model, attempt)
        if self._stopping in ("quit", "kill", "standdown"):
            return False, f"stopped by {self._stopping}", choice.model
        result = briefs.parse_receipt(receipt)
        if stage.role == "implement":
            sha = wt.commit_all(worktree, f"{tid}: implement attempt {attempt + 1}")
            self._task_update(tid, commit=sha or wt.head(worktree, "HEAD"), receipt=str(receipt))
        else:
            wt.discard_changes(worktree)        # verifiers and reviewers never edit code
        if result is None:
            return False, f"no valid receipt (exit {session.status.get('exit_code')})", choice.model
        return result.passed, "\n".join(result.findings), choice.model

    # ---- merging ---------------------------------------------------------------------

    def _merge(self, tid: str, level: str, worktree: Path) -> bool:
        done = threading.Event()
        self._merge_done[tid] = done
        self.lanes.submit(MergeJob(tid, f"jbi/{tid}", level, worktree))
        while not done.wait(0.2):
            if self._stopping in ("quit", "kill", "standdown"):
                return False
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
        self._merge_done[job.task].set()
        self._progress()
        self.wake()

    def _merge_failed(self, job: MergeJob, notes: list[str]) -> None:
        self._merge_ok[job.task] = False
        self.store.log("lane", f"{job.task} failed in a lane: {notes[0] if notes else ''}", task=job.task)
        self._merge_done[job.task].set()

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
        if not self._all_merged and all(t["status"] == "merged" for t in plan["tasks"].values()):
            self._all_merged = True
            self.hooks.all_merged()

    # ---- decisions and control -------------------------------------------------------

    def _open_decision(self, tid: str, attempts: int, findings: list[str]) -> None:
        did = f"d-{tid}-{attempts}"
        self.store.commit("decision.opened", Changes().set("decisions", did, {
            "title": f"{tid} failed {attempts} times", "body": "\n".join(findings[:10]),
            "options": ["retry", "skip", "pause"], "status": "open", "owner": "runtime", "task": tid}),
            {"decision": did, "task": tid})
        self._task_update(tid, status="decision")

    def answer(self, decision: str, answer: str) -> None:
        tid = self.store.read(f"/decisions/{decision}/task")
        if tid:
            self.control(tid, {"skip": "skip", "pause": "pause"}.get(answer, "retry"))

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
            while any(th.is_alive() for th in list(self._running.values())):
                time.sleep(0.2)
        if mode == "standdown":
            self._signal_sessions(signal.SIGINT, groups_too=False)
            self._wait_sessions(STANDDOWN_WAIT)
            for tid, thread in list(self._running.items()):
                worktree = self.run_dir / "worktrees" / tid
                if (worktree / ".git").exists():
                    try:
                        wt.commit_all(worktree, f"{tid}: work in progress (standdown)")
                    except wt.GitError as error:
                        self.store.log("task", f"{tid}: cannot commit work in progress: {error}", task=tid)
                if self.store.read(f"/plan/tasks/{tid}/status") != "merged":
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
        self.lanes.close()
        self.hooks.stopped(mode)

    def recorded(self) -> set[int]:
        """Every pid and pgid that the run recorded, for liveness checks."""
        pids = set(self.recorded_pids) | set(self.groups.seen)
        for session in self.sessions.values():
            pids.update(v for v in (session.read().get("pid"), session.status.get("shim_pid")) if v)
        return {p for p in pids if p}


__all__ = ["Supervisor", "Tmux", "alive"]
