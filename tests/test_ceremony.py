"""Ceremony lifecycle tests: leased dispatch, direct CIAdapter, retry bounds. No real service."""

from __future__ import annotations

import inspect
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from claude_mux_lite.harness import Usage
from jbi import briefs, ceremony, ci_adapter, protocol, waves, worktrees as wt
from jbi.ceremony import (
    MAX_ATTEMPTS,
    CeremonyError,
    CeremonyReceipt,
    TaskLedger,
    WindowPool,
    parse_agent_receipt,
    run_ceremony,
)
from jbi.store import Changes, Store, now
from jbi.tracking import Tracking

ROOT = Path(__file__).resolve().parents[1]


def done(out: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, out, "")


class FakeHarnessFactory:
    """Builds fake leased harnesses. `script` maps a call index to (file writes, final text)."""

    def __init__(self, script, tokens=(10, 5)):
        self.script, self.tokens, self.calls = script, tokens, []

    def __call__(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        writes, text = self.script(index, kwargs)
        factory = self

        class Harness:
            def run(self, brief):
                for rel, content in writes.items():
                    kwargs["tracking"].authorize_write(kwargs["task_id"], rel)
                    path = Path(kwargs["workdir"]) / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                factory.calls[index]["brief"] = brief
                return SimpleNamespace(final_text=text, exit_code=0,
                                       usage=Usage(factory.tokens[0], factory.tokens[1], 0, 0))

            def take_messages(self):
                return [{"role": "user", "content": "b"}, {"role": "assistant", "content": text}]

        return Harness()


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.repo = self.root / "repo"
        wt.init_repo(self.repo)
        (self.repo / "README").write_text("base\n")
        self.base = wt.commit_all(self.repo, "base")
        self.calls: list[list[str]] = []
        self.verdicts: list[str] = []
        self.pr = 930
        self.squash = self.base
        self.tracking = Tracking(self.run_dir / "fleet.db")
        self.addCleanup(self.tracking.close)
        self.pool = WindowPool(self.run_dir / "window_pool.json", windows=("work1",))

    def head(self, tid: str) -> str:
        return wt.head(self.repo, f"jbi/{tid}") or ""

    def runner(self, argv, cwd):
        self.calls.append(list(argv))
        if argv[0] == "ci-conductor":
            if argv[1] == "ready":
                self.assertEqual(argv, ["ci-conductor", "ready", str(self.pr), "--head", self.head('D01')])
                return done(json.dumps({"type": "queued", "job": "J1",
                                        "text": f"#{self.pr} queued at {self.head('D01')[:9]}"}))
            verdict = self.verdicts.pop(0) if self.verdicts else "GREEN"
            head = self.head("D01")
            lines = [f"prs: #{self.pr}@{head[:9]}", f"verdict {verdict}  failed gates: unit"]
            if verdict == "GREEN":
                lines.append(f"merge: #{self.pr}->{self.squash[:9]}; stopped=None")
            if verdict == "NOMERGE":
                lines[1] = "verdict GREEN  failed gates: none"
            return done(json.dumps({"type": "data", "job": "J1", "text": "\n".join(lines)}))
        if argv[0] == "git" and "rev-parse" in argv:
            return done(self.squash)
        if argv[:3] == ["gh", "pr", "list"]:
            return done("[]")
        if argv[:3] == ["gh", "pr", "create"]:
            return done(f"https://example.test/pull/{self.pr}")
        return done()

    def ci(self) -> ci_adapter.CIAdapter:
        return ci_adapter.CIAdapter(runner=self.runner)

    def task(self, tid="D01", owns=("src/d01.py",)) -> dict:
        return {"id": tid, "title": "Core", "goal": "Build core", "deps": [], "acceptance": "true",
                "owns": list(owns)}

    def pass_text(self, tid="D01") -> str:
        return f"BRIEF {tid} | uncommitted | PASS\nDID: wrote file"

    def go(self, factory, task=None, **kwargs):
        return run_ceremony(task or self.task(), pool=self.pool, repo=self.repo, run_dir=self.run_dir,
                            tracking=self.tracking, runner=self.runner, ci=self.ci(),
                            harness_factory=factory, **kwargs)


class DefaultsTest(unittest.TestCase):
    def test_defaults_and_public_shapes(self) -> None:
        self.assertEqual(MAX_ATTEMPTS, 8)
        sig = inspect.signature(run_ceremony)
        self.assertEqual(sig.parameters["max_attempts"].default, 8)
        self.assertIsNone(sig.parameters["reset_window_every"].default)
        self.assertEqual([f for f in ceremony.CeremonyReceipt.__dataclass_fields__],
                         ["task", "sha", "passed", "findings", "tokens"])
        self.assertEqual([f for f in ceremony.TaskOutcome.__dataclass_fields__],
                         ["task", "window", "pr_number", "head_sha", "merge_sha", "attempts", "tokens", "status"])

    def test_parse_agent_receipt(self) -> None:
        rec = parse_agent_receipt("BRIEF D01 | abc1234 | FAIL\ntokens: 12\nbad thing", "D01")
        self.assertEqual((rec.passed, rec.tokens, rec.findings), (False, 12, ["bad thing"]))
        self.assertIsNone(parse_agent_receipt("BRIEF D02 | abc1234 | PASS", "D01"))

    def test_source_names_no_obsolete_transport(self) -> None:
        text = (ROOT / "jbi" / "ceremony.py").read_text().lower()
        for banned in ("illm", "interface-llm", "mailbox", "sb_messages", "ticket start", "sb send", "board.db"):
            self.assertNotIn(banned, text)


class MergeTest(Fixture):
    def test_green_verified_merge_releases_lease_and_parks_branch(self) -> None:
        factory = FakeHarnessFactory(lambda i, k: ({"src/d01.py": "x = 1\n"}, self.pass_text()))
        outcome = self.go(factory)
        self.assertEqual((outcome.task, outcome.window, outcome.pr_number, outcome.attempts, outcome.status),
                         ("D01", "work1", 930, 1, "done"))
        self.assertEqual((outcome.tokens, outcome.merge_sha), (15, self.squash))
        fleet = self.tracking.fleet()
        self.assertEqual(fleet["tasks"][0]["state"], "merged")
        self.assertEqual(len(fleet["merges"]), 1)
        self.assertIsNotNone(fleet["leases"][0]["released_at"])
        self.assertEqual(len(fleet["dispatches"]), 1)
        self.assertEqual(fleet["dispatches"][0]["continuation"], 0)
        self.assertEqual(fleet["costs"][0]["input_tokens"], 10)
        self.assertTrue(wt.git(self.repo, "branch", "--list", "jbi-parked/D01-*"))
        self.assertEqual(self.pool.get("work1")["status"], "free")
        self.assertEqual(TaskLedger(self.run_dir / "task_ledger.jsonl").entries()[0]["merge_sha"], self.squash)
        self.assertEqual(self.tracking.session_history("work1-D01")[1]["role"], "assistant")
        for argv in self.calls:
            self.assertIn(argv[0], ("git", "gh", "ci-conductor"))
        self.assertEqual(factory.calls[0]["owns"], ("src/d01.py",))
        self.assertIs(factory.calls[0]["tracking"], self.tracking)

    def test_ci_failure_starts_fresh_same_task_correction_dispatch(self) -> None:
        self.verdicts = ["RED", "GREEN"]
        factory = FakeHarnessFactory(
            lambda i, k: ({"src/d01.py": f"x = {i}\n"}, self.pass_text()))
        outcome = self.go(factory)
        self.assertEqual((outcome.attempts, outcome.tokens), (2, 30))
        fleet = self.tracking.fleet()
        self.assertEqual([d["attempt"] for d in fleet["dispatches"]], [1, 2])
        self.assertEqual([d["continuation"] for d in fleet["dispatches"]], [0, 1])
        self.assertEqual(len(fleet["leases"]), 1)
        self.assertEqual(fleet["tasks"][0]["state"], "merged")
        self.assertIn("CI did not confirm", factory.calls[1]["brief"])
        self.assertEqual(factory.calls[1]["session"][0]["role"], "user")
        self.assertEqual(self.tracking.fleet()["ci_submissions"][-1]["state"], "green")

    def test_reset_window_every_keeps_same_task_history(self) -> None:
        self.verdicts = ["RED", "GREEN"]
        factory = FakeHarnessFactory(lambda i, k: ({"src/d01.py": f"x = {i}\n"}, self.pass_text()))
        self.go(factory, max_attempts=4, reset_window_every=1)
        self.assertEqual(factory.calls[1]["session"][0]["role"], "user")
        self.assertEqual([d["continuation"] for d in self.tracking.fleet()["dispatches"]], [0, 1])

    def test_agent_fail_receipt_retries_with_findings(self) -> None:
        def script(i, kwargs):
            if i == 0:
                return {}, "BRIEF D01 | uncommitted | FAIL\ncompile error"
            return {"src/d01.py": "ok\n"}, self.pass_text()
        factory = FakeHarnessFactory(script)
        outcome = self.go(factory)
        self.assertEqual(outcome.attempts, 2)
        self.assertIn("compile error", factory.calls[1]["brief"])


class NoMergeEvidenceTest(Fixture):
    def test_green_without_merge_record_is_never_marked_merged(self) -> None:
        self.verdicts = ["NOMERGE"] * 3
        factory = FakeHarnessFactory(lambda i, k: ({"src/d01.py": f"x = {i}\n"}, self.pass_text()))
        with self.assertRaises(CeremonyError) as caught:
            self.go(factory, max_attempts=2)
        self.assertIn("failed after 2 attempts", str(caught.exception))
        self.assertIn("no merge record", str(caught.exception))
        fleet = self.tracking.fleet()
        self.assertEqual(fleet["merges"], [])
        self.assertNotEqual(fleet["tasks"][0]["state"], "merged")

    def test_unresolvable_squash_commit_is_refused(self) -> None:
        def runner(argv, cwd):
            if argv[0] == "git" and "rev-parse" in argv:
                return done("not-a-sha")
            return self.runner(argv, cwd)
        factory = FakeHarnessFactory(lambda i, k: ({"src/d01.py": f"x = {i}\n"}, self.pass_text()))
        with self.assertRaises(CeremonyError):
            run_ceremony(self.task(), pool=self.pool, repo=self.repo, run_dir=self.run_dir,
                         tracking=self.tracking, ci=ci_adapter.CIAdapter(runner=runner),
                         harness_factory=factory, max_attempts=1)
        self.assertEqual(self.tracking.fleet()["merges"], [])


class RetryBoundTest(Fixture):
    def test_exceeding_max_attempts_abandons_clean_lease_frees_window_and_updates_store(self) -> None:
        state = protocol.initial_state("r-cer", now(), name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="tmux")
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        task = self.task()
        store.commit("plan.updated", Changes().replace("plan", waves.compile_plan([task], 1)))
        factory = FakeHarnessFactory(lambda i, k: ({}, "BRIEF D01 | uncommitted | FAIL\ncompile error"))
        with self.assertRaises(CeremonyError) as caught:
            self.go(factory, task=task, store=store, max_attempts=3)
        self.assertIn("D01", str(caught.exception))
        self.assertIn("failed after 3 attempts", str(caught.exception))
        self.assertIn("compile error", str(caught.exception))
        self.assertEqual(len(factory.calls), 3)
        self.assertEqual(self.pool.get("work1")["status"], "free")
        self.assertEqual(store.read("/plan/tasks/D01/status"), "failed")
        self.assertEqual(store.read("/plan/tasks/D01/attempts"), 3)
        self.assertIsNone(store.read("/plan/tasks/D01/window"))
        fleet = self.tracking.fleet()
        self.assertEqual(fleet["tasks"][0]["state"], "abandoned")
        self.assertEqual(len(fleet["dispatches"]), 3)
        self.assertTrue(wt.git(self.repo, "branch", "--list", "jbi-parked/D01-*"))

    def test_dirty_abandon_refusal_leaves_lease_live_and_window_busy(self) -> None:
        factory = FakeHarnessFactory(
            lambda i, k: ({"src/d01.py": f"x = {i}\n"}, "BRIEF D01 | uncommitted | FAIL\nbad"))
        with self.assertRaises(CeremonyError) as caught:
            self.go(factory, max_attempts=1)
        self.assertIn("lease stays live", str(caught.exception))
        fleet = self.tracking.fleet()
        self.assertIsNone(fleet["leases"][0]["released_at"])
        self.assertEqual(self.pool.get("work1")["status"], "busy")

    def test_unowned_change_is_discarded_and_corrected(self) -> None:
        def script(i, kwargs):
            if i == 0:
                (Path(kwargs["workdir"]) / "stray.txt").write_text("x")
                return {"src/d01.py": "a\n"}, self.pass_text()
            return {"src/d01.py": "b\n"}, self.pass_text()
        factory = FakeHarnessFactory(script)
        outcome = self.go(factory)
        self.assertEqual(outcome.attempts, 2)
        self.assertIn("modified unowned path stray.txt", factory.calls[1]["brief"])

    def test_invalid_limits_and_missing_owns_are_refused(self) -> None:
        factory = FakeHarnessFactory(lambda i, k: ({}, ""))
        with self.assertRaisesRegex(CeremonyError, "max_attempts"):
            self.go(factory, max_attempts=0)
        with self.assertRaisesRegex(CeremonyError, "reset_window_every"):
            self.go(factory, reset_window_every=0)
        with self.assertRaisesRegex(CeremonyError, "owns"):
            self.go(factory, task=self.task(owns=()))

    def test_agent_error_exit_is_a_failed_attempt(self) -> None:
        class Failing:
            def __init__(self, **kw):
                pass

            def run(self, brief):
                return SimpleNamespace(final_text="ERROR: endpoint down", exit_code=1, usage=Usage())

        with self.assertRaises(CeremonyError) as caught:
            self.go(Failing, max_attempts=1)
        self.assertIn("agent exit 1: ERROR: endpoint down", str(caught.exception))
        self.assertEqual(self.tracking.fleet()["dispatches"][0]["verdict"], "error")


class MainTest(Fixture):
    def test_main_runs_a_dag_with_injected_adapters(self) -> None:
        dag = self.root / "plan.yaml"
        dag.write_text('project: "demo"\nrepo: "./demo"\nmode: "existing"\nwaves: "auto"\ntasks:\n'
                       '  - id: "D01"\n    title: "Core"\n    goal: "Build core"\n    deps: []\n'
                       '    acceptance: "true"\n    owns: ["src/d01.py"]\n', encoding="utf-8")
        factory = FakeHarnessFactory(lambda i, k: ({"src/d01.py": "x\n"}, self.pass_text()))
        rc = ceremony.main(["--dag", str(dag), "--repo", str(self.repo), "--run-dir", str(self.run_dir / "m"),
                            "--windows", "work9", "--max-attempts", "2"],
                           runner=self.runner, harness_factory=factory, ci=self.ci())
        self.assertEqual(rc, 0)
        entries = TaskLedger(self.run_dir / "m" / "task_ledger.jsonl").entries()
        self.assertEqual((entries[0]["task"], entries[0]["pr_number"]), ("D01", 930))


if __name__ == "__main__":
    unittest.main()
