"""Unit tests for CI lane check enforcement in .jbi/commands.json."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jbi import protocol, rigor, worktrees as wt
from jbi.lanes import CheckResult, Lanes, MergeJob
from jbi.shim import Groups
from jbi.store import Store, now


class LanesCheckEnforcementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir(parents=True)
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True)
        state = protocol.initial_state(
            "r-lanes", now(), name="demo", mode="existing", target=str(self.repo),
            max_sessions=1, lanes=1, tmux="tmux",
        )
        self.store = Store(self.run_dir / "run.db", state)
        self.merged: list[tuple[str, str]] = []
        self.failed: list[tuple[str, list[str]]] = []
        self.lanes = Lanes(
            1,
            store=self.store,
            repo=self.repo,
            run_dir=self.run_dir,
            groups=Groups(),
            on_merged=lambda job, sha: self.merged.append((job.task, sha)),
            on_failed=lambda job, notes: self.failed.append((job.task, notes)),
        )
        self.lanes.start()

    def tearDown(self) -> None:
        self.lanes.close()
        self.store.close()
        self.tmp.cleanup()

    def _write_commands_raw(self, tree_dir: Path, raw_text: str) -> None:
        cfg_dir = tree_dir / ".jbi"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "commands.json").write_text(raw_text, encoding="utf-8")

    def _write_commands(self, tree_dir: Path, mapping: object) -> None:
        self._write_commands_raw(tree_dir, json.dumps(mapping))

    def test_vibe_with_no_checks_succeeds_without_commands_file(self) -> None:
        result = self.lanes.run_checks((), self.repo, lane=1)
        self.assertEqual(result, CheckResult(True, ["no lane checks for this rigor level"]))

    def test_absent_commands_file_skips_checks_for_unbootstrapped_repo(self) -> None:
        result = self.lanes.run_checks(("build", "test"), self.repo, lane=1)
        self.assertEqual(
            result,
            CheckResult(
                True,
                [
                    "build: skipped, no command in .jbi/commands.json",
                    "test: skipped, no command in .jbi/commands.json",
                ],
            ),
        )

    def test_invalid_or_non_object_commands_json_fails_closed(self) -> None:
        for bad_raw in ("{invalid json", "[]", '"just-a-string"', "42"):
            with self.subTest(raw=bad_raw):
                self._write_commands_raw(self.repo, bad_raw)
                result = self.lanes.run_checks(("build",), self.repo, lane=1)
                self.assertFalse(result.ok)
                self.assertIn(".jbi/commands.json", "\n".join(result.notes))

    def test_missing_or_blank_required_check_keys_fail_closed(self) -> None:
        all_checks = ("build", "test", "lint", "format_check")
        for missing_key in all_checks:
            for bad_value in ("__omit__", "", "   ", None, 123):
                mapping: dict[str, object] = {k: "true" for k in all_checks if k != missing_key}
                if bad_value != "__omit__":
                    mapping[missing_key] = bad_value
                with self.subTest(missing=missing_key, value=bad_value):
                    self._write_commands(self.repo, mapping)
                    result = self.lanes.run_checks(all_checks, self.repo, lane=1)
                    self.assertFalse(result.ok)
                    self.assertEqual(
                        result.notes[-1],
                        f"{missing_key} failed (missing command in .jbi/commands.json)",
                    )

    def test_merge_refuses_branch_when_rigor_lane_check_is_missing_then_merges_when_present(self) -> None:
        checks = tuple(rigor.LEVELS["high"]["lane_checks"])
        self.assertTrue(checks)

        wt.init_repo(self.repo)
        (self.repo / "app.py").write_text("v = 1\n", encoding="utf-8")
        partial = {checks[0]: "true"} if len(checks) > 1 else {}
        self._write_commands(self.repo, partial)
        base_sha = wt.commit_all(self.repo, "initial")
        self.assertIsNotNone(base_sha)

        worktree = wt.create_worktree(self.repo, "t1", str(base_sha), self.run_dir / "worktrees")
        (worktree / "app.py").write_text("v = 2\n", encoding="utf-8")
        wt.commit_all(worktree, "t1: update app")

        job = MergeJob(task="t1", branch="jbi/t1", level="high", worktree=worktree)
        merged_sha = self.lanes.merge(1, job)
        self.assertIsNone(merged_sha)
        self.assertEqual(self.merged, [])
        self.assertEqual(len(self.failed), 1)
        missing_name = checks[1] if len(checks) > 1 else checks[0]
        self.assertIn(
            f"{missing_name} failed (missing command in .jbi/commands.json)",
            "\n".join(self.failed[0][1]),
        )
        self.assertEqual(wt.head(self.repo, wt.MAIN), base_sha)

        self._write_commands(worktree, {name: "true" for name in checks})
        wt.commit_all(worktree, "t1: define all lane checks")
        merged_sha = self.lanes.merge(1, job)
        self.assertIsNotNone(merged_sha)
        self.assertEqual(len(self.merged), 1)
        self.assertEqual(wt.head(self.repo, wt.MAIN), merged_sha)


if __name__ == "__main__":
    unittest.main()
