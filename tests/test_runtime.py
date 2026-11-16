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

from jbi import briefs, protocol, waves, worktrees as wt
from jbi.lanes import Lanes, MergeJob
from jbi.shim import Groups
from jbi.store import Changes, Store
from jbi.supervisor import WINDOW_NAME, Supervisor

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


class StepWindows(RunFixture):
    """REQUIREMENT 1.1 to 1.6: one step, one window, closed and verified gone.

    Every assertion here reads the tmux server, not the runtime's own record. A
    runtime that only SAYS it closed a window passes no test in this class.
    """

    def record_opens(self, sup):
        """Wrap `tmux.open` to note the live step-window ids when each window opens."""
        opened, live_before = [], []
        original = sup.tmux.open
        step_ids = lambda: {wid for wid, name in sup.tmux._windows().items()
                            if WINDOW_NAME.match(name)}

        def recording_open(name, argv):
            live_before.append(step_ids())
            window = original(name, argv)
            opened.append(window)
            return window

        sup.tmux.open = recording_open
        return opened, live_before

    def test_two_serial_stages_use_two_windows_and_the_first_is_gone_before_the_second(self):
        """One task, two SEQUENTIAL steps: the second window opens after the first is gone.

        The four review steps of one task run at the same time by founder rule
        2026-11-16, so a test over a whole high-rigor run cannot assert "no earlier
        window is live". This test drives two steps one after the other and reads
        the tmux server between them.
        """
        if not HAS_TMUX:
            self.skipTest("tmux is not installed")
        sup = self.make([task("a")], rigor="vibe")
        opened, live_before = self.record_opens(sup)
        cmd = ["true"]
        cwd = self.tmp
        sup.run_session("a", "implement", cmd, cwd, "simulator", "sim-s", 0)
        sup.run_session("a", "verify", cmd, cwd, "simulator", "sim-s", 0)
        ids = [w.wid for w in opened if w is not None]
        self.assertEqual(len(ids), 2, "two steps must open two windows")
        self.assertNotEqual(ids[0], ids[1], "each step needs its own window")
        self.assertNotIn(ids[0], live_before[1],
                         f"window {ids[0]} was still live when the second step started")
        self.assertNotIn(ids[0], sup.tmux._windows(), "the first window must be gone")

    def test_two_stages_use_two_windows(self):
        if not HAS_TMUX:
            self.skipTest("tmux is not installed")
        sup = self.make([task("a")], rigor="medium")
        opened, _live = self.record_opens(sup)
        sup.start_execution()
        self.assertTrue(self.hooks.merged_event.wait(120), self.store.read("/timeline"))
        ids = [w.wid for w in opened if w is not None]
        self.assertGreaterEqual(len(ids), 2, "one task with agent stages must open two windows")
        self.assertEqual(len(set(ids)), len(ids), "each step needs its own window")
        names = [w.name for w in opened if w is not None and WINDOW_NAME.match(w.name)]
        self.assertEqual(names[0], names[1], "one task and one provider keep one name pattern")

    def test_every_step_window_is_closed_when_the_task_ends(self):
        if not HAS_TMUX:
            self.skipTest("tmux is not installed")
        sup = self.make([task("a")], rigor="medium")
        opened, _live = self.record_opens(sup)
        sup.start_execution()
        self.assertTrue(self.hooks.merged_event.wait(120), self.store.read("/timeline"))
        leftover = [w for w in opened
                    if w is not None and WINDOW_NAME.match(w.name) and w.wid in sup.tmux._windows()]
        self.assertEqual(leftover, [], "a step window must not outlive its step")
        # REQUIREMENT 1.6: only the receipt survives. The step scratch files are gone.
        self.assertEqual(sorted(p.name for p in (sup.run_dir / "sessions").glob("*")), [],
                         "session scratch files must be reclaimed")
        self.assertTrue(briefs.receipt_path(sup.run_dir, "a", "implement", 0).exists(),
                        "the receipt is the one thing that survives")

    def test_window_name_matches_the_required_pattern(self):
        sup = self.make([task("a"), task("b"), task("c", deps=["a", "b"])], rigor="vibe")
        for tid in ("a", "b", "c"):
            name = sup.window_name(tid, "agy")
            self.assertRegex(name, r"^[a-z0-9-]+-wave\d+-task\d+-[a-z-]+$")
        # wave 1 holds a and b; c is task 1 of wave 2.
        self.assertEqual(sup.window_name("b", "agy"), "t-wave1-task2-agy")
        self.assertEqual(sup.window_name("c", "agy"), "t-wave2-task1-agy")

    def test_dag_name_is_lowercased_with_hyphens(self):
        sup = self.make([task("a")], rigor="vibe")
        self.store.commit("run.name", Changes().set("plan", "prd", {"name": "Waves 4"}), {})
        self.assertEqual(sup.window_name("a", "openai-compatible"),
                         "waves-4-wave1-task1-openai-compatible")


class DirectivesAreDry(unittest.TestCase):
    """REQUIREMENT 3.3, 3.5 and 3.6: the common block is declared once, never copied.

    3.6 says a directive that repeats a common instruction is a defect. Requirement
    4.3 then says the render JOINS the common block to the step block, so each
    assembled prompt legitimately holds the common text once. The defect this class
    catches is a SECOND copy: one in the common block and one in the step block, or
    a copy written into a step declaration in the source.
    """

    def test_no_common_instruction_is_copied_into_any_prompt(self):
        from jbi import briefs, rigor
        sample = {"id": "a", "title": "A", "goal": "build a", "acceptance": "true", "owns": ["src/a.py"]}
        prompts = [briefs.render(task=sample, role=stage.role, stage=stage.name, attempt=0,
                                 receipt=Path("/tmp/r")) for stage in rigor.pipeline("high")]
        for _number, text in briefs.COMMON:
            for index, prompt in enumerate(prompts):
                self.assertEqual(prompt.count(text), 1,
                                 f"common instruction {text!r} appears {prompt.count(text)} "
                                 f"times in prompt {index + 1}, not once")

    def test_no_step_declaration_holds_a_common_instruction(self):
        from jbi import briefs
        step_texts = [text for _n, text in briefs.STAGE_DIRECTIVES.values()]
        step_texts += [text for _n, text in briefs.ROLE_DIRECTIVES.values()]
        for _number, common in briefs.COMMON:
            for step in step_texts:
                self.assertNotIn(common, step, f"the step block repeats the common instruction {common!r}")

    def test_the_common_block_is_declared_once_in_the_source(self):
        from jbi import briefs
        source = Path(briefs.__file__).read_text()
        for _number, text in briefs.COMMON:
            self.assertEqual(source.count(text), 1,
                             f"common instruction {text!r} is written {source.count(text)} "
                             f"times in {briefs.__file__}")

    def test_the_step_block_carries_only_the_step_instruction(self):
        from jbi import briefs
        sample = {"id": "a", "title": "A", "goal": "build a", "acceptance": "true"}
        implement = briefs.render(task=sample, role="implement", stage="implement", attempt=0,
                                  receipt=Path("/tmp/r"))
        dry_run = briefs.render(task=sample, role="review", stage="dry-run", attempt=0,
                                receipt=Path("/tmp/r"))
        # One numbered step line each, and each line is false of the other step.
        self.assertIn("Step 8.", implement)
        self.assertNotIn("Step 7.", implement)
        self.assertIn("Step 7.", dry_run)
        self.assertNotIn("Step 8.", dry_run)


class Runs(RunFixture):
    def test_dry_run_rehearses_the_landing_path_and_merges_nothing(self):
        """A dry run walks the whole pipeline and never lands anything.

        The founder asked for an in-weight rehearsal: prove the app would work
        in practice without taking the landing path. The merge stage must report
        what it would do and leave the base branch untouched.
        """
        sup = self.make([task("a", owns=["src/a.txt"])], rigor="vibe")
        self.store.commit("config", Changes().set("config", "dry_run", True), {})
        base_before = wt.head(self.repo, "main")
        sup.start_execution()
        self.assertTrue(self.hooks.merged_event.wait(60), self.store.read("/timeline"))
        self.assertEqual(self.status("a"), "merged")
        self.assertTrue(self.store.read("/plan/tasks/a/dry_run"))
        # The base branch did not move: nothing was landed.
        self.assertEqual(wt.head(self.repo, "main"), base_before)
        log = " ".join(str(row) for row in (self.store.read("/timeline") or []))
        self.assertIn("DRY RUN", log)

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
            self.assertEqual(self.store.read("/sessions/s-a-implement-1/window"), "t-wave1-task1-simulator")

    def test_high_run_merges(self):
        sup = self.make([task("a"), task("b")], rigor="high")
        sup.start_execution()
        self.assertTrue(self.hooks.merged_event.wait(90), self.store.read("/timeline"))
        stages = self.store.read("/plan/tasks/a/stages")
        # FOUNDER RULE 2026-11-16: the review step is four reviews run together,
        # followed by security, test-audit, methodology-audit, and dry-run before merge.
        self.assertEqual(list(stages), ["implement", "acceptance", "verify",
                                        "review-cascade", "review-ds-1", "review-ds-2", "review-ds-3",
                                        "security", "test-audit", "methodology-audit", "dry-run", "merge"])
        self.assertTrue(all(s["status"] == "passed" for s in stages.values()))
        for rname in ("review-cascade", "review-ds-1", "review-ds-2", "review-ds-3",
                      "test-audit", "methodology-audit", "dry-run"):
            self.assertEqual(stages[rname], {"status": "passed", "detail": "simulated result", "model": "sim-l"})


class ReviewFanout(RunFixture):
    """The four reviews run AT THE SAME TIME, not one after another.

    FOUNDER RULE 2026-11-16 and the founder's 2026-11-17 correction: stages 4, 5,
    6 and 7 are one parallel group, so the pipeline is 9 sequential steps and not
    12. A test that only checks the four stages were RECORDED proves nothing about
    timing: four sequential calls record exactly the same four rows. This test
    holds each review at a barrier until all four have arrived, so it can only
    pass when all four are in flight together.
    """

    def test_all_four_reviews_are_in_flight_at_once(self):
        sup = self.make([task("a")], rigor="high")
        FANOUT = ("review-cascade", "review-ds-1", "review-ds-2", "review-ds-3")
        barrier = threading.Barrier(4, timeout=20)
        arrived, lock = [], threading.Lock()
        original = sup._agent_stage

        def agent_stage(task_, stage, worktree, findings, *, discard=True):
            # Only the four fan-out stages, not the later audits that also carry
            # role "review": those are separate sequential steps, not this group.
            if stage.name in FANOUT:
                with lock:
                    arrived.append(stage.name)
                # Blocks until FOUR reviews have reached this point. If the reviews
                # ran one at a time, the first would time out here and the test fails.
                barrier.wait()
            return original(task_, stage, worktree, findings, discard=discard)

        sup._agent_stage = agent_stage
        sup.start_execution()
        self.assertTrue(self.hooks.merged_event.wait(120), self.store.read("/timeline"))
        self.assertEqual(sorted(set(arrived)), sorted(FANOUT),
                         "not all four review stages ran: %r" % arrived)

    def test_a_review_group_is_one_sequential_step(self):
        """The pipeline is 9 steps, because the four reviews are one step."""
        from jbi import rigor
        stages = rigor.PIPELINES["high"]
        names = [s.name for s in stages]
        self.assertEqual(len(names), 12, names)
        review_names = [s.name for s in rigor.review_stages()]
        self.assertEqual(review_names,
                         ["review-cascade", "review-ds-1", "review-ds-2", "review-ds-3"])
        # The four are adjacent in the pipeline: nothing sits between them, so they
        # form the one group the supervisor fans out.
        first = names.index("review-cascade")
        self.assertEqual(names[first:first + 4], review_names,
                         "the review stages are not adjacent, so they cannot be one group")
        self.assertEqual(len(names) - (len(review_names) - 1), 9,
                         "the sequential step count is not 9")


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

    def test_9_diff_ownership_enforcement_discards_unowned_edits(self):
        from jbi import briefs, rigor
        repo = self.tmp / "own_repo"
        wt.init_repo(repo)
        (repo / "README").write_text("base\n")
        base_sha = wt.commit_all(repo, "base")
        t = task("a", owns=["src/owned.py"])
        sup, store, hooks = self.supervisor(repo, [t])
        worktree = wt.create_worktree(repo, "a", base_sha, sup.run_dir / "worktrees")

        def bad_session(tid, role, cmd, cwd, provider, model, attempt):
            (cwd / "src").mkdir(exist_ok=True)
            (cwd / "src" / "owned.py").write_text("ok = True\n")
            (cwd / "src" / "unowned.py").write_text("bad = True\n")
            rec = briefs.receipt_path(sup.run_dir, tid, role, attempt)
            rec.parent.mkdir(parents=True, exist_ok=True)
            rec.write_text(f"BRIEF {tid} | uncommitted | PASS\ndone\n")
            return type("S", (), {"status": {"exit_code": 0}})()

        sup.run_session = bad_session
        ok, detail, _ = sup._agent_stage(store.read("/plan/tasks/a"), rigor.pipeline("vibe")[0],
                                         worktree, [])
        self.assertFalse(ok)
        self.assertIn("modified unowned path", detail)
        self.assertIn("src/unowned.py", detail)
        self.assertEqual(wt.dirty_paths(worktree), [])
        self.assertEqual(wt.head(worktree, "HEAD"), base_sha)
        self.assertFalse((worktree / "src" / "unowned.py").exists())

    def test_10_external_ci_handover_pushes_creates_pr_and_calls_illm(self):
        from jbi import ci_adapter as ci_mod
        repo = self.tmp / "ci_repo"
        wt.init_repo(repo)
        (repo / "README").write_text("base\n")
        base_sha = wt.commit_all(repo, "base")
        t = task("a", owns=["src/a.py"])
        sup, store, hooks = self.supervisor(repo, [t])
        sup.ci_service = "illm"
        worktree = wt.create_worktree(repo, "a", base_sha, sup.run_dir / "worktrees")
        (worktree / "src").mkdir(exist_ok=True)
        (worktree / "src" / "a.py").write_text("x = 1\n")
        head_sha = wt.commit_all(worktree, "a: implement attempt 1")
        sup._task_update("a", commit=head_sha)
        calls: list[list[str]] = []

        def fake_runner(argv, cwd):
            calls.append(list(argv))
            if argv[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(argv, 0, "[]", "")
            if argv[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(argv, 0, "https://github.com/org/repo/pull/812\n", "")
            if argv[:4] == ["illm", "--json", "ci", "ready"]:
                threading.Timer(0.05, lambda: sup._merged(MergeJob("a", "jbi/a", "vibe", worktree), head_sha)).start()
                return subprocess.CompletedProcess(argv, 0, json.dumps({"status": "queued", "pr": 812}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        sup.ci_adapter = ci_mod.CIAdapter(runner=fake_runner)
        ok = sup._merge("a", "vibe", worktree)
        self.assertTrue(ok)
        self.assertEqual(store.read("/plan/tasks/a/pr_number"), 812)
        self.assertEqual(store.read("/plan/tasks/a/status"), "merged")
        self.assertTrue(any(c[:3] == ["gh", "pr", "create"] and "--draft" in c for c in calls))
        self.assertIn(["illm", "--json", "ci", "ready", "812", "--head", head_sha], calls)

    def test_11_ci_notice_failure_wakes_task_and_rebriefs_with_structured_findings(self):
        from jbi import briefs, ci_adapter as ci_mod
        from jbi.engine import Engine
        repo = self.tmp / "notice_repo"
        wt.init_repo(repo)
        (repo / "README").write_text("base\n")
        wt.commit_all(repo, "base")
        t = task("a", owns=["src/a.txt"])
        sup, store, hooks = self.supervisor(repo, [t])
        store.commit("p", Changes().replace("run", "phase", "execute"))
        sup.ci_service = "illm"
        engine = Engine(store, completer=None, runtime=sup)
        ready_calls = 0

        def fake_runner(argv, cwd):
            nonlocal ready_calls
            if argv[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps([{"number": 812}]) if ready_calls else "[]", "")
            if argv[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(argv, 0, "812\n", "")
            if argv[:4] == ["illm", "--json", "ci", "ready"]:
                ready_calls += 1
                sha = argv[6]
                if ready_calls == 1:
                    threading.Timer(0.05, lambda: engine.submit({
                        "id": "n-1",
                        "type": "ci.notice",
                        "payload": {
                            "schema": "illm.ci.failure.v1",
                            "job_id": "20261116-183000-l0-gA",
                            "pr_number": 812,
                            "head_sha": sha,
                            "verdict": "red",
                            "failed_gates": ["backend-unit", "authority"],
                            "evidence_url": "file:///tmp/ev/result.json",
                            "first_failure_excerpt": "412: FAIL src/auth/authority.spec.ts",
                            "retry_allowed": True,
                        },
                    })).start()
                else:
                    threading.Timer(0.05, lambda: engine.submit({
                        "id": "n-2",
                        "type": "ci.notice",
                        "payload": {"pr_number": 812, "head_sha": sha, "verdict": "merged", "merge_sha": sha},
                    })).start()
                return subprocess.CompletedProcess(argv, 0, json.dumps({"status": "queued"}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        sup.ci_adapter = ci_mod.CIAdapter(runner=fake_runner)
        sup.start_execution()
        self.assertTrue(hooks.merged_event.wait(60), store.read("/plan/tasks/a"))
        self.assertEqual(store.read("/plan/tasks/a/status"), "merged")
        self.assertEqual(store.read("/plan/tasks/a/attempts"), 1)
        second_brief = briefs.brief_path(sup.run_dir, "a", "implement", 1).read_text()
        self.assertIn("20261116-183000-l0-gA", second_brief)
        self.assertIn("backend-unit, authority", second_brief)
        self.assertIn("file:///tmp/ev/result.json", second_brief)
        self.assertIn("412: FAIL src/auth/authority.spec.ts", second_brief)

    def test_12_ceremony_runs_full_9_step_loop_rebriefs_same_window_and_takes_next_task(self):
        from jbi import briefs
        from jbi.ceremony import CeremonyReceipt, TaskLedger, WindowPool, run_ceremony
        repo = self.tmp / "ceremony_repo"
        wt.init_repo(repo)
        (repo / "README").write_text("base\n")
        wt.commit_all(repo, "base")
        t1 = task("a", owns=["src/a.py"])
        t2 = task("b", deps=["a"], owns=["src/b.py"])
        sup, store, hooks = self.supervisor(repo, [t1, t2])
        pool = WindowPool(sup.run_dir / "window_pool.json", windows=("work1",))
        ledger = TaskLedger(sup.run_dir / "task_ledger.jsonl")
        commands: list[list[str]] = []
        busy_snapshots: list[dict] = []

        def fake_runner(argv, cwd):
            commands.append(list(argv))
            if argv[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(argv, 0, "[]", "")
            if argv[:3] == ["gh", "pr", "create"]:
                pr = "901\n" if "jbi/a" in argv else "902\n"
                return subprocess.CompletedProcess(argv, 0, pr, "")
            if argv[:4] == ["illm", "--json", "ci", "submit"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"status": "queued"}), "")
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")

        def wait_receipt(window, tid, attempt, receipt_file):
            busy_snapshots.append(pool.get(window))
            worktree = sup.run_dir / "worktrees" / tid
            (worktree / "src").mkdir(exist_ok=True)
            (worktree / "src" / f"{tid}.py").write_text(f"attempt = {attempt}\n")
            return CeremonyReceipt(task=tid, sha="uncommitted", passed=True, findings=[], tokens=500)

        def wait_ci_notice(tid, pr_number, head_sha, attempt):
            # Fail 5 times on task 'a' (> rigor.DECISION_AFTER = 4) to prove no decision card opens.
            if tid == "a" and attempt < 5:
                return {
                    "schema": "illm.ci.failure.v1",
                    "job_id": f"job-{attempt}",
                    "pr_number": pr_number,
                    "head_sha": head_sha,
                    "verdict": "red",
                    "failed_gates": ["unit"],
                    "evidence_url": f"file:///tmp/ev/{attempt}.json",
                    "first_failure_excerpt": f"failure on attempt {attempt}",
                    "retry_allowed": True,
                }
            return {"pr_number": pr_number, "head_sha": head_sha, "verdict": "green", "merge_sha": head_sha}

        outcome = run_ceremony(
            t1, next_tasks=[t2], pool=pool, ledger=ledger, repo=repo, run_dir=sup.run_dir,
            store=store, runner=fake_runner, wait_receipt=wait_receipt, wait_ci_notice=wait_ci_notice,
        )
        self.assertEqual(len(outcome), 2)
        self.assertEqual((outcome[0].task, outcome[0].window, outcome[0].pr_number, outcome[0].attempts, outcome[0].tokens),
                         ("a", "work1", 901, 6, 3000))
        self.assertEqual((outcome[1].task, outcome[1].window, outcome[1].pr_number, outcome[1].attempts, outcome[1].tokens),
                         ("b", "work1", 902, 1, 500))
        self.assertTrue(all(s["status"] == "busy" for s in busy_snapshots))
        self.assertEqual(pool.get("work1")["status"], "free")
        self.assertEqual(store.read("/decisions"), {}, "ceremony must never open a decision card")
        self.assertEqual(store.read("/plan/tasks/a/status"), "merged")
        self.assertEqual(store.read("/plan/tasks/b/status"), "merged")
        ticket_starts = [c for c in commands if c[:2] == ["ticket", "start"]]
        sb_rebriefs = [c for c in commands if c[:2] == ["sb", "send"]]
        ci_submits = [c for c in commands if c[:4] == ["illm", "--json", "ci", "submit"]]
        self.assertEqual(len(ticket_starts), 2)
        self.assertEqual(len(sb_rebriefs), 5)
        self.assertTrue(all(c[2] == "work1" for c in sb_rebriefs), "must re-brief the SAME window")
        self.assertEqual(len(ci_submits), 7)
        rebrief_text = briefs.brief_path(sup.run_dir, "a", "implement", 5).read_text()
        self.assertIn("job-4", rebrief_text)
        self.assertIn("failure on attempt 4", rebrief_text)
        entries = ledger.entries()
        self.assertEqual([e["task"] for e in entries], ["a", "b"])
        self.assertEqual([e["tokens"] for e in entries], [3000, 500])

    def test_13_ceremony_main_runs_end_to_end_with_illm_mailbox_notice(self):
        import sqlite3
        from jbi import briefs, ceremony
        repo = self.tmp / "main_repo"
        run_dir = self.tmp / "main_run"
        wt.init_repo(repo)
        (repo / "README").write_text("base\n")
        wt.commit_all(repo, "base")

        dag_file = self.tmp / "plan.yaml"
        dag_file.write_text(
            'project: "demo"\n'
            'repo: "./demo"\n'
            'mode: "existing"\n'
            'waves: "auto"\n'
            'tasks:\n'
            '  - id: "D01"\n'
            '    title: "Build core"\n'
            '    goal: "Provide core"\n'
            '    deps: []\n'
            '    acceptance: "true"\n'
            '    owns: ["src/core.py"]\n'
        )
        board_db = self.tmp / "board.db"
        with sqlite3.connect(str(board_db)) as conn:
            conn.execute("CREATE TABLE sb_messages (id INTEGER PRIMARY KEY, from_slot TEXT, to_slot TEXT, text TEXT)")
        illm_state = self.tmp / "illm_state"
        queue_dir = illm_state / "mailboxes" / "main" / "queue"
        queue_dir.mkdir(parents=True)
        submit_count = 0

        def fake_runner(argv, cwd):
            nonlocal submit_count
            if argv[:2] == ["ticket", "start"] or argv[:2] == ["sb", "send"]:
                worktree = run_dir / "worktrees" / "D01"
                (worktree / "src").mkdir(exist_ok=True)
                (worktree / "src" / "core.py").write_text(f"v = {submit_count + 1}\n")
                rec_path = briefs.receipt_path(run_dir, "D01", "implement", submit_count)
                rec_path.parent.mkdir(parents=True, exist_ok=True)
                rec_path.write_text("BRIEF D01 | uncommitted | PASS\ntokens: 800\n")
                return subprocess.CompletedProcess(argv, 0, "ok\n", "")
            if argv[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(argv, 0, "[]", "")
            if argv[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(argv, 0, "910\n", "")
            if argv[:4] == ["illm", "--json", "ci", "submit"]:
                submit_count += 1
                sha = argv[6]
                if submit_count == 1:
                    (queue_dir / "1001-red.json").write_text(json.dumps({
                        "id": "1001-red",
                        "from": "ci",
                        "to": "main",
                        "type": "failed",
                        "job": "j-red-1",
                        "text": "CI red on unit",
                        "data": {
                            "schema": "illm.ci.failure.v1",
                            "job_id": "j-red-1",
                            "pr_number": 910,
                            "head_sha": sha,
                            "verdict": "red",
                            "failed_gates": ["unit"],
                            "first_failure_excerpt": "FAIL src/core.spec.py",
                        },
                    }))
                else:
                    (queue_dir / "1002-green.json").write_text(json.dumps({
                        "id": "1002-green",
                        "from": "ci",
                        "to": "main",
                        "type": "done",
                        "job": "j-green-2",
                        "text": "PR #910 merged",
                        "data": {
                            "pr_number": 910,
                            "head_sha": sha,
                            "verdict": "merged",
                            "merge_sha": sha,
                        },
                    }))
                return subprocess.CompletedProcess(argv, 0, json.dumps({"status": "queued"}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        rc = ceremony.main(
            ["--dag", str(dag_file), "--repo", str(repo), "--run-dir", str(run_dir), "--windows", "work1"],
            runner=fake_runner,
            board_db=board_db,
            illm_state_dir=illm_state,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(submit_count, 2)
        entries = ceremony.TaskLedger(run_dir / "task_ledger.jsonl").entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["task"], entries[0]["pr_number"], entries[0]["attempts"], entries[0]["tokens"]),
                         ("D01", 910, 2, 1600))
        pool = ceremony.WindowPool(run_dir / "window_pool.json")
        self.assertEqual(pool.get("work1")["status"], "free")

    def test_14_cli_resume_reopens_stored_run_and_finishes_remaining_tasks(self):
        from jbi import cli
        repo = self.tmp / "resume_repo"
        run_dir = self.tmp / "r-20261116-resume"
        run_dir.mkdir(parents=True)
        wt.init_repo(repo)
        (repo / "README").write_text("base\n")
        wt.commit_all(repo, "base")

        state = protocol.initial_state("r-20261116-resume", "now", name="demo", mode="existing",
                                       target=str(repo), max_sessions=2, lanes=1, tmux="")
        state["config"]["rigor"] = "vibe"
        state["run"]["phase"] = "execute"
        state["bootstrap"]["status"] = "passed"
        state["bootstrap"]["branch"] = "main"
        state["accounts"] = {"simulator": {"label": "Simulator", "enabled": True, "status": "ok"}}
        plan = waves.compile_plan([task("a", owns=["src/a.txt"]), task("b", deps=["a"], owns=["src/b.txt"])], 1)
        plan["tasks"]["a"]["status"] = "merged"
        plan["tasks"]["a"]["stages"] = {"implement": {"status": "passed", "detail": "ok", "model": "sim-s"},
                                        "merge": {"status": "passed", "detail": "merged", "model": None}}
        plan["tasks"]["b"]["status"] = "running"
        state["plan"] = plan
        store = Store(run_dir / "run.db", state)
        store.close()

        args = cli.parse_args(["resume", str(run_dir), "--offline", "--no-tailscale", "--no-menu"])
        resumed = cli.Run(args, resume_dir=run_dir)
        self.addCleanup(resumed.close)
        self.addCleanup(lambda: (resumed.runtime.stop("kill"), resumed.stopped.wait(20)))
        self.assertTrue(wait_for(lambda: resumed.store.read("/run/phase") == "complete", 60),
                        resumed.store.read("/plan/tasks"))
        self.assertEqual(resumed.store.read("/plan/tasks/a/status"), "merged")
        self.assertEqual(resumed.store.read("/plan/tasks/b/status"), "merged")
        resumed.engine.submit({"id": "stop-resumed", "type": "run.stop", "payload": {"mode": "quit"}})
        self.assertTrue(resumed.stopped.wait(20))

    def test_15_dry_run_preview_cli_serves_waves_etas_and_updates_live_to_completion(self):
        import urllib.request
        from jbi import cli
        dag_file = self.tmp / "preview_dag.yaml"
        dag_file.write_text(
            'project: "preview-demo"\n'
            'repo: "./preview-demo"\n'
            'mode: "new"\n'
            'waves: "auto"\n'
            'tasks:\n'
            '  - id: "core"\n'
            '    title: "Build core"\n'
            '    goal: "Provide core"\n'
            '    deps: []\n'
            '    acceptance: "true"\n'
            '    owns: ["src/core.py"]\n'
            '  - id: "docs"\n'
            '    title: "Write docs"\n'
            '    goal: "Document core"\n'
            '    deps: []\n'
            '    acceptance: "true"\n'
            '    owns: ["docs/readme.md"]\n'
            '  - id: "cli"\n'
            '    title: "Build CLI"\n'
            '    goal: "Expose core"\n'
            '    deps: ["core"]\n'
            '    acceptance: "true"\n'
            '    owns: ["src/cli.py"]\n'
        )
        # --registry is REQUIRED here. Without it the preview run publishes to
        # ~/.tangram-dev/jbi-preview.json, which is the LIVE registry the board
        # proxy reads: a test would repoint the founder's dashboard at a fixture
        # DAG named preview_dag.yaml (found 2026-11-17 00:19).
        registry = self.tmp / "test-jbi-preview.json"
        args = cli.parse_args(["--dag", str(dag_file), "--dry-run", "--preview", "--no-tailscale",
                               "--no-menu", "--registry", str(registry)])
        run = cli.Run(args)
        self.addCleanup(run.close)
        self.addCleanup(lambda: (run.runtime.stop("kill"), run.stopped.wait(20)))
        run.store.commit("cfg", Changes().replace("config", "rigor", "vibe"))

        url = f"http://127.0.0.1:{run.server.port}/api/v1/preview"
        with urllib.request.urlopen(url, timeout=5) as resp:
            initial = json.loads(resp.read())["preview"]
        self.assertTrue(initial["dry_run"])
        self.assertEqual(initial["total_tasks"], 3)
        self.assertEqual(initial["merged_tasks"], 0)
        self.assertEqual(initial["waves_total"], 2)
        self.assertEqual(initial["waves_completed"], 0)
        self.assertTrue(initial["waves"][0]["parallel"])
        self.assertEqual(initial["critical_path"], ["core", "cli"])
        self.assertGreater(initial["remaining_eta_s"], 0)

        run.engine.submit({"id": "start-preview", "type": "run.start", "payload": {}})
        self.assertTrue(wait_for(lambda: run.store.read("/run/phase") == "complete", 60),
                        run.store.read("/plan/tasks"))

        with urllib.request.urlopen(url, timeout=5) as resp:
            final = json.loads(resp.read())["preview"]
        self.assertEqual(final["merged_tasks"], 3)
        self.assertEqual(final["waves_completed"], 2)
        self.assertEqual(final["remaining_eta_s"], 0)
        self.assertTrue(all(w["status"] == "merged" and w["remaining_eta_s"] == 0 for w in final["waves"]))
        run.engine.submit({"id": "stop-preview", "type": "run.stop", "payload": {"mode": "quit"}})
        self.assertTrue(run.stopped.wait(20))


if __name__ == "__main__":
    unittest.main()
