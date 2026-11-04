"""Runtime tests: real temp git repos, the simulator agent and private tmux servers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from jbi import protocol, waves, worktrees as wt
from jbi.lanes import Lanes, MergeJob
from jbi.shim import Groups
from jbi.store import Changes, Store
from jbi.supervisor import Supervisor

HAS_TMUX = shutil.which("tmux") is not None


def wait_for(predicate, timeout: float = 30.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:                                    # a zombie still answers kill(0); it runs nothing
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


class Hooks:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.stopped_event, self.merged_event = threading.Event(), threading.Event()

    def bootstrap_finished(self, ok, detail, commit): self.calls.append(("bootstrap", ok))
    def task_merged(self, task, commit, patch): self.calls.append(("merged", task))
    def wave_completed(self, wave): self.calls.append(("wave", wave))
    def all_merged(self): self.calls.append(("all",)); self.merged_event.set()
    def stopped(self, mode): self.calls.append(("stopped", mode)); self.stopped_event.set()


def task(tid, deps=(), owns=(), acceptance="true"):
    return {"id": tid, "title": tid, "goal": f"build {tid}", "deps": list(deps),
            "acceptance": acceptance, "owns": list(owns)}


class RunFixture(unittest.TestCase):
    """One temp repo, run dir, store and supervisor per test, each on its own tmux socket."""
    env: dict[str, str] = {}

    def make(self, tasks, rigor="vibe", max_sessions=3, use_tmux=True, supervisor=Supervisor):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-rt-"))
        self.repo, self.run_dir = self.tmp / "repo", self.tmp / "run"
        self.run_dir.mkdir()
        wt.init_repo(self.repo)
        (self.repo / "README").write_text("base\n")
        wt.commit_all(self.repo, "base")
        run_id = f"t-{uuid.uuid4().hex[:10]}"
        state = protocol.initial_state(run_id, "now", name="t", mode="new", target=str(self.repo),
                                       max_sessions=max_sessions, lanes=1, tmux="")
        state["config"]["rigor"] = rigor
        state["plan"] = waves.compile_plan(tasks, 1)
        self.store = Store(self.run_dir / "run.db", state)
        self.hooks = Hooks()
        self._saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.sup = supervisor(self.store, self.hooks, self.run_dir, self.repo, use_tmux=use_tmux)
        self.addCleanup(self.cleanup)
        return self.sup

    def cleanup(self):
        if not self.hooks.stopped_event.is_set():
            self.sup.stop("kill")
            self.hooks.stopped_event.wait(20)
        self.sup.tmux.kill_server()
        for key, value in self._saved.items():
            os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self, tid):
        return self.store.read(f"/plan/tasks/{tid}/status")


class Admission(RunFixture):
    def test_dependency_admission_and_owns_conflict(self):
        release = threading.Event()
        started: list[str] = []

        class Fake(Supervisor):
            def _run_task(inner, tid):
                started.append(tid)
                inner._task_update(tid, status="running")
                if tid == "slow":
                    release.wait(10)
                inner._task_update(tid, status="merged")

        sup = self.make([task("slow", owns=["src/a"]), task("b"), task("c", deps=["b"]),
                         task("clash", owns=["src/a"])], supervisor=Fake)
        sup._scheduler = threading.Thread(target=sup._schedule, daemon=True)
        sup._scheduler.start()
        self.assertTrue(wait_for(lambda: "c" in started, 5), "c must start once b merged")
        self.assertEqual(self.status("slow"), "running", "c started without waiting for wave 1")
        self.assertNotIn("clash", started, "clash owns src/a, which a running task owns")
        release.set()
        self.assertTrue(wait_for(lambda: "clash" in started, 5))
        sup.stop("quit")
        self.assertTrue(self.hooks.stopped_event.wait(20))


class Runs(RunFixture):
    def test_vibe_run_merges(self):
        sup = self.make([task("a", owns=["src/a.txt"]), task("b", deps=["a"])], rigor="vibe")
        sup.start_execution()
        self.assertTrue(self.hooks.merged_event.wait(60), self.store.read("/timeline"))
        self.assertEqual({self.status("a"), self.status("b")}, {"merged"})
        self.assertIn("a attempt 1", wt.git(self.repo, "show", "main:src/a.txt"))
        stages = self.store.read("/plan/tasks/a/stages")
        self.assertEqual(list(stages), ["implement", "merge"])
        self.assertIn(("wave", 2), self.hooks.calls)
        self.assertEqual(self.store.read("/telemetry/tokens_in"), 240)
        if HAS_TMUX:
            self.assertEqual(self.store.read("/sessions/s-a-implement-1/window"), "a:implement")

    def test_high_run_merges(self):
        sup = self.make([task("a"), task("b")], rigor="high")
        sup.start_execution()
        self.assertTrue(self.hooks.merged_event.wait(90), self.store.read("/timeline"))
        stages = self.store.read("/plan/tasks/a/stages")
        self.assertEqual(list(stages), ["implement", "acceptance", "verify", "review", "security", "merge"])
        self.assertTrue(all(s["status"] == "passed" for s in stages.values()))


class Failures(RunFixture):
    env = {"JBI_SIM_FAIL": "x:implement:9"}

    def test_fail_retry_escalation_decision(self):
        sup = self.make([task("x")], rigor="vibe")
        sup.start_execution()
        self.assertTrue(wait_for(lambda: self.status("x") == "decision", 90))
        models = [self.store.read(f"/sessions/s-x-implement-{n}/model") for n in range(1, 5)]
        self.assertEqual(models, ["sim-s", "sim-s", "sim-l", "sim-l"], "tier escalates after 2 FAILs")
        decision = self.store.read("/decisions/d-x-4")
        self.assertEqual((decision["owner"], decision["options"]), ("runtime", ["retry", "skip", "pause"]))
        self.assertEqual(self.store.read("/plan/tasks/x/attempts"), 4)


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-lane-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo, run_dir = self.tmp / "repo", self.tmp / "run"
        wt.init_repo(self.repo)
        (self.repo / ".jbi").mkdir()
        (self.repo / ".jbi" / "commands.json").write_text(json.dumps({"build": "true", "test": "true"}))
        wt.commit_all(self.repo, "base")
        worktree = wt.create_worktree(self.repo, "t", wt.head(self.repo), run_dir / "worktrees")
        (worktree / "t.txt").write_text("t\n")
        wt.commit_all(worktree, "t")
        self.store = Store(run_dir / "run.db", {"lanes": {}})
        self.merged: list[str] = []
        self.lanes = Lanes(1, store=self.store, repo=self.repo, run_dir=run_dir, groups=Groups(),
                           on_merged=lambda job, sha: self.merged.append(sha),
                           on_failed=lambda job, notes: self.fail(notes))
        self.lanes._status = lambda n, **values: None
        self.job = MergeJob("t", "jbi/t", "medium", worktree)
        (run_dir / "lanes").mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.store.close)

    def test_base_moved_recheck_and_cache_hit(self):
        original, moved = self.lanes.run_checks, []

        def run_checks(checks, tree_dir, lane):
            result = original(checks, tree_dir, lane)
            if not moved:                       # another lane merges while this one tests
                (self.repo / "other.txt").write_text("other\n")
                moved.append(wt.commit_all(self.repo, "other"))
            return result
        self.lanes.run_checks = run_checks
        sha = self.lanes.merge(1, self.job)
        self.assertEqual(self.lanes.check_runs, 2, "a moved base must run the checks again")
        self.assertEqual(wt.git(self.repo, "rev-parse", f"{sha}^"), moved[0])
        self.assertEqual(wt.head(self.repo), sha)
        self.assertEqual(sorted(wt.git(self.repo, "ls-tree", "--name-only", sha).split()),
                         [".jbi", "other.txt", "t.txt"])
        base, head = moved[0], wt.head(self.repo, "jbi/t")
        self.lanes.checked(base, head, ("build", "test"), self.repo)
        self.assertEqual(self.lanes.check_runs, 2, "the same inputs never run twice")


class StopModes(RunFixture):
    env = {"JBI_SIM_SLOW": "4"}

    def start_and_stop(self, mode, use_tmux=True, rigor="vibe"):
        sup = self.make([task("a"), task("b"), task("c")], rigor=rigor, use_tmux=use_tmux)
        sup.start_execution()
        self.assertTrue(wait_for(lambda: sum(1 for s in list(sup.sessions.values()) if s.read().get("pid")) == 3, 20))
        pids = sup.recorded()
        self.assertGreaterEqual(len(pids), 6)
        sup.stop(mode)
        self.assertTrue(self.hooks.stopped_event.wait(60), f"{mode} did not finish")
        self.assertTrue(wait_for(lambda: all(gone(p) for p in sup.recorded()), 10),
                        [p for p in sup.recorded() if not gone(p)])
        return sup

    def test_quit(self):
        self.start_and_stop("quit")
        self.assertIn(("stopped", "quit"), self.hooks.calls)

    def test_kill(self):
        self.start_and_stop("kill")

    def test_standdown(self):
        self.start_and_stop("standdown")
        self.assertEqual({self.status(t) for t in "abc"}, {"paused"})

    def test_finish(self):
        os.environ["JBI_SIM_SLOW"] = "0.5"
        self.env = {"JBI_SIM_SLOW": "0.5"}
        self.start_and_stop("finish")
        self.assertEqual({self.status(t) for t in "abc"}, {"merged"})

    def test_quit_without_tmux(self):
        sup = self.start_and_stop("quit", use_tmux=False)
        self.assertIsNone(self.store.read("/sessions/s-a-implement-1/window"))


class TargetFollowsTheUser(unittest.TestCase):
    """Regression: the runtime must build in the folder chosen at stack.accept, not the default."""

    def test_bootstrap_uses_the_accepted_target(self):
        from jbi.engine import Engine
        tmp = Path(tempfile.mkdtemp(prefix="jbi-target-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        default, chosen = tmp / "default", tmp / "chosen"
        state = protocol.initial_state(f"t-{uuid.uuid4().hex[:10]}", "now", name="t", mode="new",
                                       target=str(default), max_sessions=1, lanes=1, tmux="")
        state["run"]["phase"] = "stack"
        state["accounts"] = {"simulator": {"label": "Simulator", "enabled": True, "status": "ok"}}
        run_dir = tmp / "run"
        run_dir.mkdir()
        store = Store(run_dir / "run.db", state)
        self.addCleanup(store.close)
        hooks = Hooks()
        sup = Supervisor(store, hooks, run_dir, use_tmux=False)     # no pinned repo, as in the CLI
        self.addCleanup(lambda: (sup.stop("kill"), hooks.stopped_event.wait(20)))
        engine = Engine(store, completer=None, runtime=sup)
        hooks.bootstrap_finished = engine.bootstrap_finished
        engine.submit({"id": "accept", "type": "stack.accept", "payload": {"target": str(chosen)}})
        deadline = time.time() + 60
        while time.time() < deadline and store.read("/bootstrap/status") == "running":
            time.sleep(0.2)
        self.assertEqual(store.read("/bootstrap/status"), "passed", store.read("/bootstrap/detail"))
        self.assertTrue((chosen / ".git").exists())
        self.assertTrue((chosen / ".jbi" / "commands.json").exists())
        self.assertFalse(default.exists())


class ShimParentDeath(unittest.TestCase):
    def test_shim_kills_agent_when_parent_dies(self):
        tmp = Path(tempfile.mkdtemp(prefix="jbi-shim-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        spec = {"cmd": [sys.executable, "-c", "import time; time.sleep(60)"], "cwd": str(tmp),
                "provider": "simulator", "parent_pid": parent.pid, "status": str(tmp / "status.json"),
                "log": str(tmp / "log"), "grace": 1}
        (tmp / "spec.json").write_text(json.dumps(spec))
        root = str(Path(__file__).resolve().parent.parent)
        shim = subprocess.Popen([sys.executable, "-m", "jbi.shim", str(tmp / "spec.json")], cwd=root,
                                env={**os.environ, "PYTHONPATH": root}, start_new_session=True)
        self.addCleanup(lambda: shim.poll() is None and shim.kill())
        self.assertTrue(wait_for(lambda: (tmp / "status.json").exists(), 10))
        agent = json.loads((tmp / "status.json").read_text())["pid"]
        self.assertFalse(gone(agent))
        parent.kill()
        parent.wait()
        self.assertEqual(shim.wait(timeout=10) != 0, True)
        self.assertTrue(wait_for(lambda: gone(agent), 5), "the agent outlived the CLI process")
        self.assertEqual(json.loads((tmp / "status.json").read_text())["reason"], "parent-gone")


class StoredKeyReachesTheAgent(unittest.TestCase):
    """A stored API key reaches the agent's environment, and never the spec file or the log."""

    def test_key_in_agent_env_not_on_disk(self):
        tmp = Path(tempfile.mkdtemp(prefix="jbi-key-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        key = "test-key-" + uuid.uuid4().hex
        # A stand-in keychain: sitecustomize patches jbi.providers.child_key_env in the shim process.
        (tmp / "site").mkdir()
        (tmp / "site" / "sitecustomize.py").write_text(
            "import jbi.providers as p\n"
            f"p.child_key_env = lambda name: {{'ANTHROPIC_API_KEY': {key!r}}} if name == 'claude' else {{}}\n")
        seen = tmp / "seen.txt"
        spec = {"cmd": [sys.executable, "-c",
                        f"import os; open({str(seen)!r}, 'w').write(os.environ.get('ANTHROPIC_API_KEY', ''))"],
                "cwd": str(tmp), "provider": "claude", "parent_pid": os.getpid(),
                "status": str(tmp / "status.json"), "log": str(tmp / "log"), "grace": 1}
        (tmp / "spec.json").write_text(json.dumps(spec))
        root = str(Path(__file__).resolve().parent.parent)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp / "site"), root])}
        env.pop("ANTHROPIC_API_KEY", None)
        subprocess.run([sys.executable, "-m", "jbi.shim", str(tmp / "spec.json")], cwd=root, env=env,
                       timeout=30, start_new_session=True)
        self.assertEqual(seen.read_text(), key, "the agent did not receive the stored key")
        for path in (tmp / "spec.json", tmp / "log", tmp / "status.json"):
            self.assertNotIn(key, path.read_text(), f"the key reached {path.name}")


class ReviewFindings(unittest.TestCase):
    """Regressions for review findings 1, 2, 3, 5, 6 and 8."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-fix-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def supervisor(self, repo=None, tasks=None):
        state = protocol.initial_state(f"t-{uuid.uuid4().hex[:10]}", "now", name="t", mode="new",
                                       target=str(repo or self.tmp), max_sessions=1, lanes=1, tmux="")
        state["config"]["rigor"] = "vibe"
        if tasks:
            state["plan"] = waves.compile_plan(tasks, 1)
        store = Store(self.tmp / f"run-{uuid.uuid4().hex[:6]}.db", state)
        self.addCleanup(store.close)
        hooks = Hooks()
        sup = Supervisor(store, hooks, self.tmp / "run", repo, use_tmux=False)
        (self.tmp / "run").mkdir(exist_ok=True)
        self.addCleanup(lambda: (sup.stop("kill"), hooks.stopped_event.wait(20)))
        return sup, store, hooks

    def test_1_lane_survives_missing_binary_and_reports(self):
        repo, run = self.tmp / "repo", self.tmp / "lrun"
        run.mkdir()
        wt.init_repo(repo)
        (repo / ".jbi").mkdir()
        (repo / ".jbi/commands.json").write_text(json.dumps({"build": "no-such-binary-xyz && true", "test": "true"}))
        wt.commit_all(repo, "base")
        wtp = wt.create_worktree(repo, "a", wt.head(repo), run / "worktrees")
        (wtp / "a.txt").write_text("a")
        wt.commit_all(wtp, "a")
        store = Store(run / "run.db", {"lanes": {}})
        self.addCleanup(store.close)
        calls = []
        lanes = Lanes(1, store=store, repo=repo, run_dir=run, groups=Groups(),
                      on_merged=lambda j, s: calls.append(("merged", s)),
                      on_failed=lambda j, n: calls.append(("failed", n)))
        lanes.start()
        lanes.submit(MergeJob("a", "jbi/a", "medium", wtp))
        self.assertTrue(wait_for(lambda: calls, 20), "on_failed never ran")
        self.assertEqual(calls[0][0], "failed")
        self.assertIn("127", " ".join(calls[0][1]))
        boom = MergeJob("b", "jbi/a", "no-such-level", wtp)       # _merge raises KeyError
        lanes.submit(boom)
        self.assertTrue(wait_for(lambda: len(calls) == 2, 20))
        self.assertEqual(calls[1][0], "failed")
        self.assertTrue(lanes._threads[0].is_alive(), "the lane thread died")
        lanes.close()

    def test_2_kill_never_signals_a_reused_pid(self):
        sup, store, hooks = self.supervisor()
        victim = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(lambda: victim.poll() is None and victim.kill())
        sup.recorded_pids.add(victim.pid)
        sup.__dict__.setdefault("identities", {})["s-old"] = {victim.pid: "1"}            # recorded start time differs
        sup.stop("kill")
        self.assertTrue(hooks.stopped_event.wait(20))
        time.sleep(0.5)
        self.assertIsNone(victim.poll(), "kill mode signalled an unrelated process")

    def test_3_commands_die_with_the_parent(self):
        marker = str(uuid.uuid4().int % 10**8).zfill(8)
        script = self.tmp / "parent.py"
        script.write_text(
            "import sys, threading, time, os, signal, json\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
            "from pathlib import Path\n"
            "from jbi.shim import Groups, run_command\n"
            "from jbi.lanes import Lanes\n"
            "from jbi.agents import child_env\n"
            "g = Groups()\n"
            f"cmd = 'sleep 47.{marker}1; true'\n"
            "threading.Thread(target=run_command, args=(cmd,), kwargs=dict(cwd=Path('/tmp'), env=child_env(None), timeout=900, groups=g), daemon=True).start()\n"
            f"tree = Path({str(self.tmp)!r}) / 'tree'; (tree / '.jbi').mkdir(parents=True, exist_ok=True)\n"
            f"(tree / '.jbi/commands.json').write_text(json.dumps({{'build': 'sleep 47.{marker}2; true'}}))\n"
            "lanes = Lanes(1, store=None, repo=tree, run_dir=tree, groups=g, on_merged=None, on_failed=None)\n"
            "lanes._log = lambda *a: None\n"
            "threading.Thread(target=lanes.run_checks, args=(('build',), tree, 1), daemon=True).start()\n"
            "print('ready', flush=True); time.sleep(60)\n")
        parent = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, start_new_session=True)
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        self.assertEqual(parent.stdout.readline().strip(), b"ready")

        def alive_sleeps():
            out = subprocess.run(["pgrep", "-f", f"sleep 47.{marker}"], capture_output=True, text=True).stdout
            return [int(p) for p in out.split()]
        self.assertTrue(wait_for(lambda: len(alive_sleeps()) >= 2, 10), "the commands did not start")
        parent.kill()
        parent.wait()
        died = wait_for(lambda: not alive_sleeps(), 3)
        left = alive_sleeps()
        for pid in left:
            os.kill(pid, 9)
        self.assertTrue(died, f"commands outlived the parent by 3 s: {left}")

    def test_3_kill_is_synchronous(self):
        sup, store, hooks = self.supervisor()
        proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        from jbi.shim import process_start
        sup.__dict__.setdefault("identities", {})["s-live"] = {proc.pid: process_start(proc.pid)}
        sup.stop("kill")
        time.sleep(0.05)
        self.assertEqual(proc.poll(), -9, "stop('kill') returned before its SIGKILL")

    def test_3_run_command_missing_binary_is_127(self):
        from jbi.shim import run_command
        result = run_command("no-such-binary-xyz", cwd=self.tmp, env={"PATH": "/usr/bin:/bin"}, timeout=10)
        self.assertEqual(result.returncode, 127)

    def test_5_existing_repo_on_master_and_detached_head(self):
        repo = self.tmp / "master"
        repo.mkdir()
        wt.git(repo, "init", "-q", "-b", "master")
        (repo / "x").write_text("x")
        wt.commit_all(repo, "x")
        sup, store, hooks = self.supervisor(repo, [task("a", owns=["src/a.txt"])])
        store.commit("bootstrap.status", Changes().set("bootstrap", "branch", wt.init_repo(repo)))
        sup.start_execution()
        self.assertTrue(hooks.merged_event.wait(60), store.read("/plan/tasks/a"))
        self.assertIn("a attempt 1", wt.git(repo, "show", "master:src/a.txt"))
        self.assertIsNone(wt.head(repo, "main"))
        detached = self.tmp / "detached"
        detached.mkdir()
        wt.git(detached, "init", "-q", "-b", "main")
        (detached / "y").write_text("y")
        wt.commit_all(detached, "y")
        wt.git(detached, "checkout", "-q", "--detach")
        sup2, store2, hooks2 = self.supervisor(detached)
        sup2._bootstrap_safe()
        self.assertEqual(store2.read("/bootstrap/status"), "failed")
        self.assertIn("detached", store2.read("/bootstrap/detail"))
        self.assertIn(("bootstrap", False), hooks2.calls)

    def test_5_task_exception_marks_failed_once(self):
        repo = self.tmp / "r"
        wt.init_repo(repo)
        (repo / "x").write_text("x")
        wt.commit_all(repo, "x")
        sup, store, hooks = self.supervisor(repo, [task("a")])
        calls = []
        original = wt.create_worktree

        def broken(*a, **k):
            calls.append(1)
            raise wt.GitError("boom")
        wt.create_worktree = broken
        self.addCleanup(setattr, wt, "create_worktree", original)
        sup.start_execution()
        self.assertTrue(wait_for(lambda: store.read("/plan/tasks/a/status") == "failed", 10))
        time.sleep(2.5)
        self.assertEqual(len(calls), 1, "a failed task was admitted again")
        self.assertIn("boom", store.read("/plan/tasks/a/findings")[0])

    def test_6_pause_resume_skip(self):
        repo = self.tmp / "r"
        wt.init_repo(repo)
        (repo / "x").write_text("x")
        wt.commit_all(repo, "x")
        sup, store, hooks = self.supervisor(repo, [task("a"), task("b", deps=["a"])])
        store.commit("t", Changes().replace("config", "rigor", "medium")
                     .replace("plan", "tasks", "a", "attempts", 2))
        gate = threading.Event()
        seen = []
        original = sup._run_stage

        def run_stage(task_, stage, level, worktree, findings):
            seen.append(stage.name)
            if stage.name == "implement":
                sup.control("a", "pause")
                return True, "ok", "m"
            return original(task_, stage, level, worktree, findings) if stage.name != "review" else (True, "ok", "m")
        sup._run_stage = run_stage
        sup.start_execution()
        self.assertTrue(wait_for(lambda: store.read("/plan/tasks/a/status") == "paused"
                                 and not sup._running["a"].is_alive(), 10), store.read("/plan/tasks/a"))
        self.assertEqual(seen, ["implement"], "pause did not stop after the current stage")
        self.assertEqual(store.read("/plan/tasks/a/status"), "paused")
        self.assertEqual(store.read("/plan/tasks/a/attempts"), 2)
        sup._run_stage = lambda t, st, lv, w, f: (seen.append(st.name), (False, "nope", "m"))[1]
        sup.control("a", "resume")
        self.assertTrue(wait_for(lambda: len(seen) >= 2, 10))
        self.assertEqual(seen[1], "acceptance", "resume did not continue from the next stage")
        self.assertTrue(wait_for(lambda: store.read("/plan/tasks/a/attempts") >= 3, 10))
        self.assertGreaterEqual(store.read("/plan/tasks/a/attempts"), 3, "resume reset the attempts")
        sup.stop("quit")
        self.assertTrue(hooks.stopped_event.wait(20))
        sup.control("a", "skip")
        self.assertEqual(store.read("/plan/tasks/a/status"), "skipped")
        self.assertEqual(store.read("/plan/tasks/b/status"), "blocked")
        self.assertEqual(store.read("/plan/tasks/b/blocked_reason"), "dependency a was skipped")

    def test_8_bootstrap_missing_command_fails_and_retry(self):
        from jbi import bootstrap
        root = self.tmp / "b"
        (root / ".jbi").mkdir(parents=True)
        (root / ".jbi/commands.json").write_text(json.dumps({"build": "true"}))
        ok, detail = bootstrap.check(root)
        self.assertFalse(ok)
        self.assertIn("lint", detail)
        (root / ".jbi/commands.json").write_text("{}")
        self.assertFalse(bootstrap.check(root)[0])
        sup, store, hooks = self.supervisor(self.tmp / "nb")
        sup._bootstrap = lambda: (_ for _ in ()).throw(RuntimeError("bang"))
        sup._bootstrap_safe()
        self.assertEqual(store.read("/bootstrap/status"), "failed")
        self.assertIn("bang", store.read("/bootstrap/detail"))
        sup.retry_bootstrap()
        self.assertTrue(wait_for(lambda: store.read("/bootstrap/status") == "failed"
                                 and hooks.calls.count(("bootstrap", False)) == 2, 10))


if __name__ == "__main__":
    unittest.main()
