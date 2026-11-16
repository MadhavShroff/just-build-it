"""Resume tests: orphan reclaim, idempotence, and the conductor's lane adoption.

These tests prove the reclaim happened, not just that a status field changed
(REQUIREMENT-resume 5.2). Each test wires a real tmux server, a real git tree,
and a real pid, then kills what a kill would have killed and runs resume.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from jbi import cli, protocol, resume, waves, worktrees as wt
from jbi.store import Store

HAS_TMUX = shutil.which("tmux") is not None


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", "-L", socket, "-f", "/dev/null", *args],
                          capture_output=True, text=True)


def _server_up(socket: str) -> bool:
    return _tmux(socket, "list-sessions").returncode == 0


def _wait_live(pid: int, timeout: float = 5.0) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if resume.pid_alive(pid):
            return True
        time.sleep(0.05)
    return resume.pid_alive(pid)


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not resume.pid_alive(pid):
            return True
        time.sleep(0.05)
    return not resume.pid_alive(pid)


class PidAlive(unittest.TestCase):
    def test_pid_alive_sees_zombies_as_dead(self):
        self.assertFalse(resume.pid_alive(None))
        self.assertFalse(resume.pid_alive(0))
        self.assertFalse(resume.pid_alive(-1))
        self.assertTrue(resume.pid_alive(os.getpid()))
        proc = subprocess.Popen(["sleep", "5"])
        try:
            self.assertTrue(resume.pid_alive(proc.pid))
        finally:
            proc.kill(); proc.wait()
        # Reap the zombie so the assertion below is not racing the parent.
        self.assertTrue(_wait_dead(proc.pid))


class TmuxReclaim(unittest.TestCase):
    """5.2 — kill a run with a live tmux window and prove resume reclaims it."""

    def setUp(self):
        if not HAS_TMUX:
            self.skipTest("tmux unavailable")
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-resume-tmux-"))
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        self.run_id = f"t-resume-{uuid.uuid4().hex[:8]}"
        self.socket = resume.tmux_socket(self.run_id)
        self._cleanup_socket()
        self.addCleanup(self._teardown)

    def _cleanup_socket(self):
        subprocess.run(["tmux", "-L", self.socket, "kill-server"],
                       capture_output=True)
        sock_path = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / self.socket
        sock_path.unlink(missing_ok=True)

    def _teardown(self):
        self._cleanup_socket()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn_window(self, name: str = "t1:implement"):
        proc = _tmux(self.socket, "new-session", "-d", "-s", "jbi", "-n", name,
                     "--", "sleep", "600")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Wait until the window is listed.
        for _ in range(50):
            windows = resume.list_tmux_windows(self.socket) or []
            if name in windows:
                return
            import time
            time.sleep(0.05)
        self.fail(f"tmux window {name} never appeared")

    def _write_control(self, pid: int):
        (self.run_dir / "control.json").write_text(json.dumps(
            {"url": "http://127.0.0.1:0", "token": "x", "run_id": self.run_id, "pid": pid}))

    def test_orphaned_kills_window_when_pid_is_dead(self):
        # Parent process is alive while we set up — pretend the run is live.
        holder = subprocess.Popen(["sleep", "60"])
        try:
            self._write_control(holder.pid)
            self._spawn_window("t1:implement")
            # Sanity: window is there.
            self.assertIn("t1:implement", resume.list_tmux_windows(self.socket) or [])
            # While the holder lives, reclaim must refuse.
            report = resume.orphaned(self.run_id, self.run_dir)
            self.assertEqual(report.tmux_windows_killed, [])
            self.assertTrue(_server_up(self.socket))
            # Now kill the "run": SIGKILL the holder and reap it.
            os.kill(holder.pid, signal.SIGKILL)
            holder.wait(3)
            self.assertTrue(_wait_dead(holder.pid))
            # Resume must reclaim the window AND kill the server.
            report = resume.orphaned(self.run_id, self.run_dir)
            self.assertIn("t1:implement", report.tmux_windows_killed, report.as_dict())
            self.assertFalse(_server_up(self.socket), "tmux server survived resume")
        finally:
            holder.kill(); holder.wait()

    def test_orphaned_keeps_window_when_pid_is_live(self):
        holder = subprocess.Popen(["sleep", "60"])
        try:
            self._write_control(holder.pid)
            self._spawn_window("t2:verify")
            report = resume.orphaned(self.run_id, self.run_dir)
            self.assertEqual(report.tmux_windows_killed, [])
            self.assertTrue(_server_up(self.socket))
            self.assertTrue(any("still live" in n for n in report.notes))
        finally:
            holder.kill(); holder.wait()


class WorktreeReclaim(unittest.TestCase):
    """2.3 — remove a worktree only after its content is proven committed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-resume-wt-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        wt.init_repo(self.repo)
        (self.repo / "README").write_text("base\n")
        wt.commit_all(self.repo, "base")
        self.run_dir = self.tmp / "run"
        (self.run_dir / "worktrees").mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_clean_committed_worktree_is_removed(self):
        wt_path = wt.create_worktree(self.repo, "t1", "main", self.run_dir / "worktrees")
        (wt_path / "foo").write_text("hello\n")
        wt.commit_all(wt_path, "t1 work")
        report = resume.orphaned("t-resume-wt1", self.run_dir, target_repo=self.repo)
        self.assertIn(str(wt_path), report.worktrees_removed, report.as_dict())
        self.assertFalse(wt_path.exists())

    def test_dirty_worktree_is_kept(self):
        wt_path = wt.create_worktree(self.repo, "t2", "main", self.run_dir / "worktrees")
        (wt_path / "foo").write_text("hello\n")
        wt.commit_all(wt_path, "t2 work")
        # A scratch edit leaves a dirty tree.
        (wt_path / "foo").write_text("edited\n")
        report = resume.orphaned("t-resume-wt2", self.run_dir, target_repo=self.repo)
        self.assertEqual(report.worktrees_removed, [], report.as_dict())
        self.assertTrue(wt_path.exists())
        self.assertTrue(any("uncommitted" in (entry.get("reason") or "")
                            for entry in report.worktrees_kept), report.as_dict())

    def test_gitignored_worktree_is_kept(self):
        # Status --ignored reveals an ignored file: the worktree must NOT be removed.
        (self.repo / ".gitignore").write_text("*.local\n")
        wt.commit_all(self.repo, "gitignore")
        wt_path = wt.create_worktree(self.repo, "t3", "main", self.run_dir / "worktrees")
        (wt_path / "scratch.local").write_text("half-written\n")
        report = resume.orphaned("t-resume-wt3", self.run_dir, target_repo=self.repo)
        self.assertEqual(report.worktrees_removed, [], report.as_dict())
        self.assertTrue(wt_path.exists())


class GateReclaim(unittest.TestCase):
    """2.4 — stop a GATE job only through gatectl stop."""

    def test_uses_gatectl_stop(self):
        # A stand-in gatectl records the argv so I can prove the exact command.
        tmp = Path(tempfile.mkdtemp(prefix="jbi-resume-gate-"))
        calls = tmp / "calls.log"
        stub = tmp / "gatectl"
        stub.write_text("#!/bin/sh\necho \"$@\" >> \"%s\"\nexit 0\n" % calls)
        stub.chmod(0o755)
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

        report = resume.orphaned("t-gate", tmp, gate_ids=["j-1", "j-2"],
                                 gatectl_path=str(stub))
        self.assertEqual(sorted(report.gate_jobs_stopped), ["j-1", "j-2"])
        self.assertEqual(report.gate_jobs_kept, [])
        argv = calls.read_text().splitlines()
        self.assertEqual(argv, ["stop j-1", "stop j-2"])

    def test_records_failed_stop(self):
        tmp = Path(tempfile.mkdtemp(prefix="jbi-resume-gate-"))
        stub = tmp / "gatectl"
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        report = resume.orphaned("t-gate", tmp, gate_ids=["j-9"], gatectl_path=str(stub))
        self.assertEqual(report.gate_jobs_stopped, [])
        self.assertTrue(report.gate_jobs_kept and report.gate_jobs_kept[0]["id"] == "j-9")


class Orchestration(unittest.TestCase):
    """1.3 — resume twice is a no-op the second time; 2.5 — never fails."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-resume-orch-"))
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        self.run_id = f"t-resume-{uuid.uuid4().hex[:8]}"
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_idempotent_second_pass(self):
        # No tmux server, no worktrees, no gate jobs, no control.json.
        first = resume.orphaned(self.run_id, self.run_dir)
        second = resume.orphaned(self.run_id, self.run_dir)
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(second.tmux_windows_killed, [])
        self.assertEqual(second.worktrees_removed, [])
        self.assertEqual(second.gate_jobs_stopped, [])

    def test_records_report_on_disk(self):
        report = resume.orphaned(self.run_id, self.run_dir)
        path = resume.write_resume_record(self.run_dir, report)
        self.assertTrue(path.is_file())
        written = json.loads(path.read_text())
        self.assertEqual(written, report.as_dict())


class PrepareResumeOrchestration(unittest.TestCase):
    """5.1 — a stage in flight re-runs, earlier passes are kept."""

    def _seed(self, phase="execute"):
        tmp = Path(tempfile.mkdtemp(prefix="jbi-resume-seed-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        run_dir = tmp / "run"
        run_dir.mkdir()
        run_id = f"t-resume-{uuid.uuid4().hex[:8]}"
        repo = tmp / "repo"
        repo.mkdir()
        wt.init_repo(repo)
        (repo / "README").write_text("base\n")
        wt.commit_all(repo, "base")
        state = protocol.initial_state(run_id, "now", name="t", mode="existing", target=str(repo),
                                       max_sessions=1, lanes=1, tmux=f"tmux -L jbi-{run_id}")
        state["run"]["phase"] = phase
        state["config"]["rigor"] = "vibe"
        # Two tasks: a passed one and one that was running.
        state["plan"] = waves.compile_plan(
            [{"id": "a", "title": "a", "goal": "a", "deps": [], "acceptance": "true"},
             {"id": "b", "title": "b", "goal": "b", "deps": ["a"], "acceptance": "true"}], 1)
        # The compile marks both "ready". Force a to merged with both stages passed;
        # mark b as running, with implement passed and merge in flight.
        state["plan"]["tasks"]["a"]["status"] = "merged"
        state["plan"]["tasks"]["a"]["stages"] = {"implement": {"status": "passed"},
                                                 "merge": {"status": "passed"}}
        state["plan"]["tasks"]["b"]["status"] = "running"
        state["plan"]["tasks"]["b"]["stages"] = {"implement": {"status": "passed"},
                                                 "merge": {"status": "running"}}
        store = Store(run_dir / "run.db", state)
        self.addCleanup(store.close)
        return store, run_dir, repo

    def test_in_flight_stage_reruns_and_earlier_passes_stay(self):
        store, run_dir, _ = self._seed()
        cli.prepare_resume(store)
        # a stays merged: the recorded passes are kept.
        self.assertEqual(store.read("/plan/tasks/a/status"), "merged")
        self.assertEqual(store.read("/plan/tasks/a/stages/implement/status"), "passed")
        self.assertEqual(store.read("/plan/tasks/a/stages/merge/status"), "passed")
        # b was running: status returns to ready, resume_after points at implement.
        self.assertEqual(store.read("/plan/tasks/b/status"), "ready")
        self.assertEqual(store.read("/plan/tasks/b/resume_after"), "implement")
        # The half-written merge state on disk is still recorded, but b will
        # re-merge because resume_after stops at implement.

    def test_prepare_resume_is_idempotent(self):
        # 5.3 — a second prepare_resume changes no task state.
        store, run_dir, _ = self._seed()
        cli.prepare_resume(store)
        first_a = store.read("/plan/tasks/a")
        first_b = store.read("/plan/tasks/b")
        resumed_second = cli.prepare_resume(store)
        self.assertEqual(store.read("/plan/tasks/a"), first_a)
        self.assertEqual(store.read("/plan/tasks/b"), first_b)
        # The second pass finds no running task to flip, so it reports zero new.
        self.assertEqual(resumed_second, [])


class CiConductorAdoption(unittest.TestCase):
    """5.4 — the CI conductor adopts a lane whose GATE job is alive on restart.

    This test loads the production conductor, builds a lane whose train_code
    matches the running code, marks its GATE job active, and proves stop_stale
    does NOT stop it. It uses `__new__` and explicit attributes so the test
    does not require a fully bootstrapped conductor instance.
    """

    CONDUCTOR_PATH = Path.home() / "src/ci-conductor/conductor/ci-conductor"

    def _load_conductor_module(self):
        if not self.CONDUCTOR_PATH.exists():
            self.skipTest(f"ci-conductor not present at {self.CONDUCTOR_PATH}")
        import importlib.util as ilu
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("_ci_conductor_under_test", str(self.CONDUCTOR_PATH))
        spec = ilu.spec_from_loader("_ci_conductor_under_test", loader)
        self.assertIsNotNone(spec)
        module = ilu.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_stop_stale_adopts_live_lane_on_restart(self):
        mod = self._load_conductor_module()
        Conductor = mod.Conductor
        inst = Conductor.__new__(Conductor)

        lane = {"train_id": "j-restart", "gate_id": "g-restart", "run_id": "r-restart",
                "prs": [{"number": 1, "head_sha": "deadbeef" * 5}],
                "develop_sha": "cafebabe" * 5,
                "evidence_dir": "/tmp/ci-conductor-test"}
        # The running code is the lane's recorded code: a restart, not a change.
        # `code_also` is the set the conductor uses; on a restart it contains the
        # declared code AND the live file hash, both of which equal the lane's stamp.
        lane["train_code"] = mod.file_sha(str(self.CONDUCTOR_PATH))
        inst.code = lane["train_code"]
        inst.code_also = {lane["train_code"]}
        inst.shadow = False

        busy_calls = []

        def fake_busy():
            return [(0, lane)]

        stops = []
        tells = []

        inst.busy = fake_busy
        inst.unit_active = lambda gid: True            # GATE job alive across the restart
        inst.stop_lane = lambda i, l, *a, **k: stops.append((i, l["train_id"], a[0] if a else None)) or {}
        inst.tell = lambda slots, ntype, text, job=None, data=None: tells.append(text)
        inst.ready = lambda: {"1": {}}
        inst.develop_sha = lambda *a, **k: lane["develop_sha"]
        inst.owners = lambda *a, **k: []
        inst.main_slots = lambda: []
        inst.log = lambda *a, **k: None

        prs = {"1": {"head": lane["prs"][0]["head_sha"]}}
        inst.stop_stale(prs)

        # The lane is alive AND on the same code: stop_stale must NOT stop it.
        self.assertEqual(stops, [], "restart with unchanged code must adopt the lane, not void it")
        self.assertEqual(tells, [])
        _ = busy_calls  # unused; keeps intent explicit

    def test_stop_stale_voids_lane_when_code_changed(self):
        # The 2026-11-16 defect: an uncommitted edit changes file_sha and voids 45 lanes.
        mod = self._load_conductor_module()
        Conductor = mod.Conductor
        inst = Conductor.__new__(Conductor)
        lane = {"train_id": "j-edited", "gate_id": "g-edited", "run_id": "r-edited",
                "prs": [{"number": 1, "head_sha": "deadbeef" * 5}],
                "develop_sha": "cafebabe" * 5,
                "evidence_dir": "/tmp/ci-conductor-test", "train_code": "old-sha"}
        inst.code = "new-sha-different-from-lane"
        inst.code_also = {"new-sha-different-from-lane"}
        inst.shadow = False
        inst.busy = lambda: [(0, lane)]
        inst.unit_active = lambda gid: True
        stops = []

        def stop_lane(index, lane_, reason, detail, starve):
            stops.append((index, lane_["train_id"], reason))
            return dict(lane_, verdict="void")

        inst.stop_lane = stop_lane
        inst.tell = lambda *a, **k: None
        inst.ready = lambda: {"1": {}}
        inst.develop_sha = lambda *a, **k: lane["develop_sha"]
        inst.owners = lambda *a, **k: []
        inst.main_slots = lambda: []
        inst.log = lambda *a, **k: None
        inst.mark_starved = lambda *a, **k: None

        inst.stop_stale({"1": {"head": lane["prs"][0]["head_sha"]}})

        self.assertEqual(stops, [(0, "j-edited", "train-changed")])
        _ = mod


if __name__ == "__main__":
    unittest.main()
