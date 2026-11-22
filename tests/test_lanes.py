"""Unit tests for static CI declaration checks in .jbi/commands.json. Checks never merge."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jbi import rigor, worktrees as wt
from jbi.lanes import CheckResult, run_checks


class RunChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir(parents=True)

    def _write_raw(self, raw_text: str) -> None:
        (self.repo / ".jbi").mkdir(parents=True, exist_ok=True)
        (self.repo / ".jbi" / "commands.json").write_text(raw_text, encoding="utf-8")

    def _write(self, mapping: object) -> None:
        self._write_raw(json.dumps(mapping))

    def test_no_checks_succeeds_without_commands_file(self) -> None:
        self.assertEqual(run_checks((), self.repo), CheckResult(True, ["no lane checks for this rigor level"]))

    def test_absent_commands_file_skips_checks(self) -> None:
        self.assertEqual(run_checks(("build", "test"), self.repo), CheckResult(True, [
            "build: skipped, no command in .jbi/commands.json",
            "test: skipped, no command in .jbi/commands.json"]))

    def test_invalid_or_non_object_commands_json_fails_closed(self) -> None:
        for bad_raw in ("{invalid json", "[]", '"just-a-string"', "42"):
            with self.subTest(raw=bad_raw):
                self._write_raw(bad_raw)
                result = run_checks(("build",), self.repo)
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
                    self._write(mapping)
                    result = run_checks(all_checks, self.repo)
                    self.assertFalse(result.ok)
                    self.assertEqual(result.notes[-1],
                                     f"{missing_key} failed (missing command in .jbi/commands.json)")

    def test_checks_run_and_never_merge_a_branch(self) -> None:
        checks = tuple(rigor.LEVELS["high"]["lane_checks"])
        self.assertTrue(checks)
        wt.init_repo(self.repo)
        self._write({name: "true" for name in checks})
        base_sha = wt.commit_all(self.repo, "initial")
        worktree = wt.create_worktree(self.repo, "t1", str(base_sha), Path(self.tmp.name) / "wts")
        (worktree / "app.py").write_text("v = 2\n", encoding="utf-8")
        wt.commit_all(worktree, "t1: update app")
        result = run_checks(checks, worktree)
        self.assertTrue(result.ok, result.notes)
        self.assertEqual(result.notes, [f"{name}: passed" for name in checks])
        self.assertEqual(wt.head(self.repo, wt.MAIN), base_sha, "run_checks never lands a commit")

    def test_a_failing_command_is_reported_with_its_exit_code(self) -> None:
        self._write({"build": "exit 7"})
        result = run_checks(("build",), self.repo)
        self.assertFalse(result.ok)
        self.assertIn("build failed (7)", result.notes[0])


if __name__ == "__main__":
    unittest.main()
