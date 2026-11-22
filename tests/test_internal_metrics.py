"""Internal metrics: built from the fleet store only."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from jbi import metrics, worktrees as wt
from jbi.tracking import Tracking

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
SECTIONS = ("sessions", "tasks", "dispatches", "leases", "ci_submissions", "merges", "messages", "costs")


class MissingStoreTest(unittest.TestCase):
    def test_absent_store_is_unavailable_with_empty_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fleet = metrics.read_tracking(Path(tmp))
            self.assertFalse(fleet["available"])
            for name in SECTIONS:
                self.assertEqual(fleet[name], [])
            self.assertIsNone(fleet["cache_hit_rate"])
            self.assertFalse((Path(tmp) / "fleet.db").exists(), "a read must not create the store")
            data = metrics.collect_metrics(Path(tmp))
            self.assertIsNone(data["cache"])
            self.assertIsNone(data["averages"]["tokens_per_task"])
            self.assertIsNone(data["averages"]["time_per_task_s"])
            self.assertIsNone(data["tokens"]["total"])
            self.assertIsNone(data["over_time"]["tokens_per_day"])
            self.assertFalse(data["tokens"]["available"])
            self.assertTrue(data["tokens"]["path"].endswith("fleet.db"))

    def test_no_run_dir(self) -> None:
        self.assertFalse(metrics.collect_metrics(None)["store"]["available"])

    def test_damaged_store_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "fleet.db").write_bytes(b"not a database" * 50)
            data = metrics.collect_metrics(Path(tmp))
            self.assertFalse(data["store"]["available"])


class StoreDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name)
        self.repo = self.run_dir / "repo"
        wt.init_repo(self.repo)
        (self.repo / "f").write_text("x")
        self.base = wt.commit_all(self.repo, "init")
        self.tracking = Tracking(self.run_dir / "fleet.db")
        self.addCleanup(self.tracking.close)

    def make_task(self, tid: str, path: str):
        self.tracking.create_task(title=tid, owns=[path], branch=f"jbi/{tid}", worktree=self.repo,
                                  base_commit=self.base, task_id=tid)
        return self.tracking.dispatch(tid, session_id=f"s-{tid}", owns=[path], branch=f"jbi/{tid}",
                                      worktree=self.repo)

    def test_costs_and_merges_feed_averages(self) -> None:
        one = self.make_task("t1", "a")
        self.tracking.record_cost(one["dispatch_id"], input_tokens=100, output_tokens=50,
                                  cache_read=30, cache_write=10, cost_usd=0.5)
        self.tracking.record_ci("t1", pr_number=5, head_sha=SHA, base_commit=self.base,
                                service_reply={"type": "queued"}, green_run="job1", state="green")
        self.tracking.record_merge("t1", 5, SHA, "b" * 40, "job1")
        data = metrics.collect_metrics(self.run_dir)
        self.assertTrue(data["store"]["available"])
        self.assertEqual(data["averages"]["tokens_per_task"], 150)
        self.assertIsNotNone(data["averages"]["time_per_task_s"])
        self.assertEqual(data["tokens"]["total"], 150)
        self.assertEqual(data["prs_merged"]["total"], 1)
        self.assertEqual(data["task_ledger"]["completed_count"], 1)
        self.assertEqual(data["cache"], {"read": 30, "write": 10, "hit_rate": 0.75})
        self.assertEqual(len(data["merges"]), 1)
        self.assertTrue(data["generated_at"].endswith("Z"))
        self.assertRegex(data["generated_at"], r"\.\d{3}Z$")

    def test_unknown_values_are_null_not_zero(self) -> None:
        one = self.make_task("t1", "a")
        self.tracking.record_cost(one["dispatch_id"])
        data = metrics.collect_metrics(self.run_dir)
        self.assertIsNone(data["tokens"]["total"])
        self.assertIsNone(data["averages"]["tokens_per_task"])
        self.assertIsNone(data["cache"])
        self.assertEqual(data["costs"][0]["input_tokens"], None)
        self.assertEqual(data["over_time"]["tokens_per_day"], [])

    def test_no_merge_means_no_average(self) -> None:
        self.make_task("t1", "a")
        data = metrics.collect_metrics(self.run_dir)
        self.assertIsNone(data["averages"]["time_per_task_s"])
        self.assertEqual(data["prs_merged"]["total"], 0)

    def test_payload_keys(self) -> None:
        self.make_task("t1", "a")
        data = metrics.collect_metrics(self.run_dir)
        for key in ("generated_at", "sessions", "tasks", "dispatches", "leases", "ci_submissions",
                    "merges", "messages", "costs", "cache", "tokens", "task_ledger", "prs_merged",
                    "over_time", "averages"):
            self.assertIn(key, data)
        for tile in ("tokens", "task_ledger", "prs_merged"):
            for field in ("available", "status", "path", "error"):
                self.assertIn(field, data[tile])
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(len(data["leases"]), 1)


class SourceGuardTest(unittest.TestCase):
    def test_metrics_is_small_and_standard_library_only(self) -> None:
        path = ROOT / "jbi" / "metrics.py"
        text = path.read_text()
        self.assertLessEqual(len(text.splitlines()), 200)
        allowed = {"__future__", "datetime", "pathlib", "typing"}
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Import):
                self.assertTrue({a.name.split(".")[0] for a in node.names} <= allowed)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                self.assertIn((node.module or "").split(".")[0], allowed)
        lowered = text.lower()
        for banned in ("glm", "board.db", "subprocess", ".claude", "tangram", "ledger.jsonl"):
            self.assertNotIn(banned, lowered)


if __name__ == "__main__":
    unittest.main()
