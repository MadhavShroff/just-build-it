"""Direct neutral dispatch and evidence-only merge evidence in the real DAG supervisor."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from jbi import briefs, ci_adapter, protocol, rigor, waves, worktrees as wt
from jbi.store import Store, Changes
from jbi.supervisor import Supervisor


class RecordedCI:
    """A test CI service. It records evidence but never merges a working tree."""

    def __init__(self, squash: str):
        self.squash, self.calls = squash, []

    def handover(self, repo, task, *, base, tracking):
        self.calls.append((task["id"], "ready"))
        return ci_adapter.HandoverResult(task["id"], f"jbi/{task['id']}", 901,
                                         task["commit"], {"type": "queued", "job": "J1"}, base)

    def pending(self, repo, result):
        return False

    def confirm_merge(self, repo, result, *, tracking):
        self.calls.append((result.task, "job"))
        tracking.record_ci(result.task, pr_number=result.pr_number, head_sha=result.head_sha,
                           base_commit=wt.head(repo, result.base_commit), squash_sha=self.squash,
                           green_run="J1", state="green",
                           service_reply={"type": "data", "text": "verdict GREEN"})
        tracking.mark_task_state(result.task, "merging")
        tracking.record_merge(result.task, result.pr_number, result.head_sha, self.squash, "J1")
        return {"squash_sha": self.squash}


class Hooks:
    def __init__(self):
        self.merged = []

    def task_merged(self, task, commit, patch):
        self.merged.append((task, commit))

    def bootstrap_finished(self, *args):
        pass

    wave_completed = all_merged = stopped = bootstrap_finished


class NeutralDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.run_dir = Path(self.tmp.name) / "run"
        self.repo.mkdir()
        self.run_dir.mkdir()
        wt.init_repo(self.repo)
        (self.repo / "README").write_text("base\n")
        self.base = wt.commit_all(self.repo, "base")
        task = {"id": "t1", "title": "Core", "goal": "write source", "deps": [],
                "owns": ["src/t1.txt"], "acceptance": "true", "model": "sim-l"}
        state = protocol.initial_state("r", "now", name="demo", mode="new", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["bootstrap"]["branch"] = "main"
        state["config"]["rigor"] = "vibe"
        state["plan"] = waves.compile_plan([{**task, "override": {"pin": {"provider": "claude", "model": "neutral"}}}], 1)
        state["accounts"]["claude"] = {"enabled": True, "status": "ok"}
        self.store = Store(self.run_dir / "run.db", state)
        self.addCleanup(lambda: self.store.close())
        self.hooks = Hooks()
        self.sup = Supervisor(self.store, self.hooks, self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(self.sup.tracking.close)
        self.worktree = wt.create_worktree(self.repo, "t1", self.base, self.run_dir / "worktrees")

    def test_new_task_spec_has_fresh_history_and_a_granted_lease(self):
        leaf = self.sup._task_lease("t1", self.worktree)
        brief, receipt = briefs.write(self.run_dir, task=self.store.read("/plan/tasks/t1"),
                                     role="implement", stage="implement", attempt=0)
        path = self.sup._store_lite_spec("t1", "implement", self.worktree, "foo-model", 0, brief, receipt,
                                         self.run_dir / "sessions/s-t1-implement-1", 300)
        spec = json.loads(path.read_text())
        self.assertFalse(spec["continuation"])
        self.assertEqual(spec["owns"], ["src/t1.txt"])
        self.assertIsNone(leaf["released_at"])
        self.assertEqual(self.sup.tracking.fleet()["dispatches"][0]["continuation"], 0)

    def test_new_task_does_not_receive_other_sessions_history(self):
        self.sup._task_lease("t1", self.worktree)
        other = wt.create_worktree(self.repo, "t2", self.base, self.run_dir / "worktrees")
        self.sup.tracking.create_task(title="Other", owns=["src/other.txt"], branch="jbi/t2",
                                     worktree=other, base_commit=self.base, task_id="t2")
        self.sup.tracking.dispatch("t2", session_id="s-other-t2", owns=["src/other.txt"], branch="jbi/t2",
                                   worktree=other)
        self.sup.tracking.release("t2", reason="abandon", abandon=True)
        self.sup.tracking.record_session_history("s-other-t2", [{"role": "user", "content": "other task"}])
        brief, receipt = briefs.write(self.run_dir, task=self.store.read("/plan/tasks/t1"),
                                     role="implement", stage="implement", attempt=0)
        path = self.sup._store_lite_spec("t1", "implement", self.worktree, "model", 0, brief, receipt,
                                         self.run_dir / "sessions/s-t1-implement-1", 300)
        self.assertFalse(json.loads(path.read_text())["continuation"])

    def test_same_task_correction_retains_history(self):
        self.sup._task_lease("t1", self.worktree)
        session = self.sup._stage_dispatch("s-t1-implement-1", "t1", self.worktree, "m", attempt=0)["session_id"]
        self.sup.tracking.record_session_history(session, [{"role": "user", "content": "first task"}])
        path = self.sup._store_lite_spec(
            "t1", "implement", self.worktree, "model", 1,
            *briefs.write(self.run_dir, task=self.store.read("/plan/tasks/t1"), role="implement", stage="implement", attempt=1),
            self.run_dir / "sessions/s-t1-implement-2", 300)
        spec = json.loads(path.read_text())
        self.assertTrue(spec["continuation"])
        self.assertIn("t1", spec["session_id"])

    def test_supervisor_lease_is_live_before_real_neutral_execution_and_usage_survives(self):
        from tests.test_lite_process import FakeEndpoint, SECRET, final, write_call
        from unittest import mock
        call = write_call({"input_tokens": 7, "output_tokens": 2, "cache_read": 20, "cache_creation": 8})
        call["content"][0]["input"] = {"path": "src/t1.txt", "content": "hello\n"}
        usage = {"input_tokens": 7, "output_tokens": 2, "cache_read_input_tokens": 20,
                 "cache_creation_input_tokens": 8}
        self.store.commit("account.status", Changes().set("accounts", "claude", {"enabled": True, "status": "ok"}))
        task = self.store.read("/plan/tasks/t1")
        self.store.commit("task.override",
                          Changes().set("plan", "tasks", "t1", "override",
                                        {"pin": {"provider": "claude", "model": "neutral"}}), {})
        with FakeEndpoint([call, final("BRIEF t1 | uncommitted | PASS", usage)]) as server:
            with mock.patch.dict(os.environ, {"CLAUDE_MUX_BASE_URL": server.url, "CLAUDE_MUX_API_KEY": SECRET}):
                good, detail, model = self.sup._agent_stage(
                    self.store.read("/plan/tasks/t1"), rigor.stage("vibe", "implement"), self.worktree, [])
        self.assertTrue(good, detail)
        self.assertTrue((self.worktree / "src/t1.txt").is_file(), self.store.read("/sessions"))
        dispatches = self.sup.tracking.fleet()["dispatches"]
        self.assertIsNotNone(dispatches[0]["started_at"])
        self.assertEqual(len(dispatches), 2)
        self.assertEqual([d["continuation"] for d in dispatches], [0, 0])
        self.assertEqual(dispatches[-1]["verdict"], "passed")
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(self.sup.tracking.fleet()["costs"][0]["cache_read"], 20)

    def test_direct_ci_marks_verified_merge_only(self):
        self.sup._task_lease("t1", self.worktree)
        (self.worktree / "src").mkdir()
        (self.worktree / "src/t1.txt").write_text("hello\n")
        head = wt.commit_all(self.worktree, "t1")
        self.sup._task_update("t1", commit=head)
        self.sup.tracking.record_commit("t1", head)
        self.sup.ci_adapter = RecordedCI(self.base)
        self.assertTrue(self.sup._merge("t1", "vibe", self.worktree))
        self.assertEqual(self.hooks.merged, [("t1", self.base)])
        self.assertTrue(self.sup.tracking.fleet()["merges"])
        self.sup._reclaim_worktree("t1", self.worktree)
        self.assertTrue(self.sup.tracking.fleet()["leases"][0]["released_at"])

    def test_ci_resume_reuses_exact_pending_handover(self):
        from tests.test_runtime import FakeCI
        service = FakeCI(self.repo, self.run_dir / "fake-service")
        self.sup.ci_adapter = service.adapter
        self.sup._task_lease("t1", self.worktree)
        (self.worktree / "src").mkdir(exist_ok=True)
        (self.worktree / "src/t1.txt").write_text("waiting\n")
        head = wt.commit_all(self.worktree, "t1: ready")
        self.sup._task_update("t1", commit=head)
        self.sup.tracking.record_commit("t1", head)
        handover = self.sup.ci_adapter.handover(self.repo, self.store.read("/plan/tasks/t1"),
                                                base="main", tracking=self.sup.tracking)
        self.sup._handovers["t1"] = handover
        report = service.jobs[handover.reply["job"]]
        self.assertIsNone(report["squash"])
        self.assertEqual(self.store.read("/plan/tasks/t1/status"), "ready")
        self.store.close()
        from jbi import cli
        self.store = Store(self.run_dir / "run.db")
        task = self.store.read("/plan/tasks/t1")
        self.store.commit("task.stage", Changes().set("plan", "tasks", "t1", "stages",
                          {"implement": {"status": "passed"}}).set("plan", "tasks", "t1", "status", "merging"))
        cli.prepare_resume(self.store)
        resumed = Supervisor(self.store, self.hooks, self.run_dir, self.repo, use_tmux=False,
                             ci_adapter=service.adapter)
        self.addCleanup(resumed.tracking.close)
        original = report["squash"]
        self.assertIsNone(original)
        resumed.start_execution()
        deadline = __import__("time").monotonic() + 15
        while __import__("time").monotonic() < deadline:
            record = resumed.tracking.fleet()["tasks"][0]
            if record["state"] == "merged":
                break
            __import__("time").sleep(0.1)
        self.assertEqual(record["state"], "merged")
        ready_calls = [argv for argv in service.calls if "ready" in argv]
        self.assertEqual(len(ready_calls), 1, ready_calls)
        self.assertEqual(len(resumed.tracking.fleet()["dispatches"]), 1)
        deadline = __import__("time").monotonic() + 15
        while __import__("time").monotonic() < deadline:
            lease = resumed.tracking.fleet()["leases"][0]
            if lease["released_at"] or self.store.read("/plan/tasks/t1/lease_state") == "held":
                break
            __import__("time").sleep(0.1)
        self.assertIsNotNone(lease["released_at"],
                             self.store.read("/plan/tasks/t1/release_error"))
        resumed.stop("kill")


if __name__ == "__main__":
    unittest.main()
