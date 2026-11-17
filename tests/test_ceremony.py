"""Unit tests for task ceremony retry bounds, non-retryable notices, window resets, and CI polling."""

from __future__ import annotations

import inspect
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jbi import briefs, ceremony, protocol, waves, worktrees as wt
from jbi.ceremony import (
    MAX_ATTEMPTS,
    CeremonyError,
    CeremonyReceipt,
    WindowPool,
    poll_ci_notice,
    run_ceremony,
)
from jbi.store import Changes, Store, now


def _completed(argv: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode=returncode, stdout=stdout, stderr="")


class CeremonyRetryAndPollingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir(parents=True)
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_defaults_match_ceremony_signature_and_polling_intervals(self) -> None:
        self.assertEqual(MAX_ATTEMPTS, 8)

        ceremony_sig = inspect.signature(run_ceremony)
        self.assertEqual(ceremony_sig.parameters["max_attempts"].default, 8)
        self.assertIsNone(ceremony_sig.parameters["reset_window_every"].default)

        poll_sig = inspect.signature(poll_ci_notice)
        self.assertEqual(poll_sig.parameters["poll_interval"].default, 1.0)
        self.assertEqual(poll_sig.parameters["status_interval"].default, 15.0)

    def test_exceeding_max_attempts_frees_window_updates_store_and_raises(self) -> None:
        wt.init_repo(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "owned.py").write_text("x = 1\n", encoding="utf-8")
        (self.repo / "src" / "other.py").write_text("y = 1\n", encoding="utf-8")
        wt.commit_all(self.repo, "initial")

        state = protocol.initial_state(
            "r-ceremony", now(), name="demo", mode="existing", target=str(self.repo),
            max_sessions=1, lanes=1, tmux="tmux",
        )
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        task = {
            "id": "D02",
            "title": "Owned module",
            "goal": "Update owned module",
            "deps": [],
            "acceptance": "true",
            "owns": ["src/owned.py"],
        }
        store.commit("plan.updated", Changes().replace("plan", waves.compile_plan([task], 1)))

        pool = WindowPool(self.run_dir / "window_pool.json", windows=("work2",))
        dispatched: list[list[str]] = []

        def runner(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            if argv[:1] in (["ticket"], ["sb"]):
                dispatched.append(list(argv[:3]))
                return _completed(argv)
            if argv[:3] == ["gh", "pr", "list"]:
                return _completed(argv, stdout=json.dumps([{"number": 42}]))
            if argv[:3] == ["illm", "--json", "ci"]:
                return _completed(argv, stdout=json.dumps({"queued": True}))
            return _completed(argv)

        def wait_receipt(window: str, tid: str, attempt: int, receipt_file: Path) -> CeremonyReceipt:
            worktree = self.run_dir / "worktrees" / tid
            if attempt == 0:
                (worktree / "src" / "other.py").write_text("y = 2\n", encoding="utf-8")
                return CeremonyReceipt(task=tid, sha="uncommitted", passed=True)
            if attempt == 1:
                return CeremonyReceipt(task=tid, sha="uncommitted", passed=False, findings=["compile error"])
            (worktree / "src" / "owned.py").write_text(f"x = {attempt + 2}\n", encoding="utf-8")
            return CeremonyReceipt(task=tid, sha="uncommitted", passed=True)

        def wait_ci_notice(tid: str, pr_number: int, head_sha: str, attempt: int) -> dict[str, object]:
            return {
                "verdict": "red",
                "retry_allowed": True,
                "failed_gates": ["unit"],
                "first_failure_excerpt": "assertion failed",
            }

        with self.assertRaises(CeremonyError) as caught:
            run_ceremony(
                task,
                pool=pool,
                repo=self.repo,
                run_dir=self.run_dir,
                store=store,
                runner=runner,
                wait_receipt=wait_receipt,
                wait_ci_notice=wait_ci_notice,
                max_attempts=4,
            )

        self.assertIn("D02", str(caught.exception))
        self.assertIn("4", str(caught.exception))
        self.assertEqual(pool.snapshot()["windows"]["work2"]["status"], "free")
        self.assertEqual(store.read("/plan/tasks/D02/status"), "failed")
        self.assertEqual(store.read("/plan/tasks/D02/attempts"), 4)
        self.assertIsNone(store.read("/plan/tasks/D02/window"))
        self.assertEqual(
            dispatched,
            [
                ["ticket", "start", "work2"],
                ["sb", "send", "work2"],
                ["sb", "send", "work2"],
                ["sb", "send", "work2"],
            ],
        )

    def test_non_retryable_ci_notice_stops_immediately_and_frees_window(self) -> None:
        state = protocol.initial_state(
            "r-nonretry", now(), name="demo", mode="existing", target=str(self.repo),
            max_sessions=1, lanes=1, tmux="tmux",
        )
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        task = {"id": "D09", "title": "Auth policy", "goal": "Enforce policy", "deps": [], "acceptance": "true"}
        store.commit("plan.updated", Changes().replace("plan", waves.compile_plan([task], 1)))

        pool = WindowPool(self.run_dir / "window_pool.json", windows=("work1",))
        dispatched: list[list[str]] = []

        def runner(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            if argv[:1] in (["ticket"], ["sb"]):
                dispatched.append(list(argv[:3]))
            if argv[:3] == ["gh", "pr", "list"]:
                return _completed(argv, stdout=json.dumps([{"number": 77}]))
            return _completed(argv)

        with self.assertRaises(CeremonyError) as caught:
            run_ceremony(
                task,
                pool=pool,
                repo=self.repo,
                run_dir=self.run_dir,
                store=store,
                runner=runner,
                wait_receipt=lambda w, t, a, p: CeremonyReceipt(task=t, sha="abc12345", passed=True),
                wait_ci_notice=lambda t, pr, sha, a: {
                    "verdict": "merge-blocked",
                    "data": {"retry_allowed": False, "first_failure_excerpt": "protected branch rule"},
                },
            )

        self.assertIn("D09", str(caught.exception))
        self.assertEqual(pool.snapshot()["windows"]["work1"]["status"], "free")
        self.assertEqual(store.read("/plan/tasks/D09/status"), "failed")
        self.assertEqual(store.read("/plan/tasks/D09/attempts"), 1)
        self.assertIsNone(store.read("/plan/tasks/D09/window"))
        self.assertEqual(dispatched, [["ticket", "start", "work1"]])

    def test_reset_window_every_and_cli_flags_reset_poisoned_window_via_ticket_start(self) -> None:
        wt.init_repo(self.repo)
        (self.repo / "README").write_text("base\n", encoding="utf-8")
        wt.commit_all(self.repo, "base")

        dag_file = self.root / "plan.yaml"
        dag_file.write_text(
            'project: "demo"\n'
            'repo: "./demo"\n'
            'mode: "existing"\n'
            'waves: "auto"\n'
            'tasks:\n'
            '  - id: "D03"\n'
            '    title: "Reset recovery"\n'
            '    goal: "Recover after window reset"\n'
            '    deps: []\n'
            '    acceptance: "true"\n'
            '    owns: ["src/d03.py"]\n',
            encoding="utf-8",
        )
        board_db = self.root / "board.db"
        with sqlite3.connect(str(board_db)) as conn:
            conn.execute("CREATE TABLE sb_messages (id INTEGER PRIMARY KEY, from_slot TEXT, to_slot TEXT, text TEXT)")
        illm_state = self.root / "illm_state"
        queue_dir = illm_state / "mailboxes" / "main" / "queue"
        queue_dir.mkdir(parents=True)

        dispatched: list[list[str]] = []
        call_index = 0

        def runner(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            nonlocal call_index
            if argv[:2] in (["ticket", "start"], ["sb", "send"]):
                dispatched.append(list(argv[:3]))
                worktree = self.run_dir / "worktrees" / "D03"
                (worktree / "src").mkdir(exist_ok=True)
                rec_path = briefs.receipt_path(self.run_dir, "D03", "implement", call_index)
                rec_path.parent.mkdir(parents=True, exist_ok=True)
                if call_index < 2:
                    rec_path.write_text("BRIEF D03 | uncommitted | FAIL\npoisoned context\n", encoding="utf-8")
                else:
                    (worktree / "src" / "d03.py").write_text("ok = True\n", encoding="utf-8")
                    rec_path.write_text("BRIEF D03 | uncommitted | PASS\ntokens: 400\n", encoding="utf-8")
                call_index += 1
                return _completed(argv, stdout="ok\n")
            if argv[:3] == ["gh", "pr", "list"]:
                return _completed(argv, stdout="[]")
            if argv[:3] == ["gh", "pr", "create"]:
                return _completed(argv, stdout="920\n")
            if argv[:4] == ["illm", "--json", "ci", "submit"]:
                sha = argv[6]
                (queue_dir / "2001-green.json").write_text(
                    json.dumps({
                        "id": "2001-green",
                        "from": "ci",
                        "type": "done",
                        "data": {"pr_number": 920, "head_sha": sha, "verdict": "merged", "merge_sha": sha},
                    }),
                    encoding="utf-8",
                )
                return _completed(argv, stdout=json.dumps({"status": "queued"}))
            return _completed(argv)

        rc = ceremony.main(
            [
                "--dag", str(dag_file),
                "--repo", str(self.repo),
                "--run-dir", str(self.run_dir),
                "--windows", "work3",
                "--max-attempts", "4",
                "--reset-window-every", "2",
            ],
            runner=runner,
            board_db=board_db,
            illm_state_dir=illm_state,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            dispatched,
            [
                ["ticket", "start", "work3"],
                ["sb", "send", "work3"],
                ["ticket", "start", "work3"],
            ],
        )

    def test_poll_ci_notice_throttles_status_runner_and_checks_mailbox_each_tick(self) -> None:
        state_dir = self.root / "illm"
        queue_dir = state_dir / "mailboxes" / "main" / "queue"
        queue_dir.mkdir(parents=True)
        clock = [0.0]
        sleeps: list[float] = []
        status_calls: list[float] = []

        def fake_monotonic() -> float:
            return clock[0]

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds
            if len(sleeps) == 3:
                (queue_dir / "001.json").write_text(
                    json.dumps({
                        "from": "ci",
                        "type": "done",
                        "data": {"pr_number": 77, "head_sha": "deadbeef00", "verdict": "merged"},
                    }),
                    encoding="utf-8",
                )

        def runner(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            status_calls.append(clock[0])
            return _completed(argv, stdout=json.dumps({"status": "running", "head_sha": "deadbeef00"}))

        with mock.patch.object(ceremony.time, "monotonic", side_effect=fake_monotonic), \
                mock.patch.object(ceremony.time, "sleep", side_effect=fake_sleep):
            verdict = poll_ci_notice(
                "D04",
                77,
                "deadbeef00",
                0,
                run_dir=self.run_dir,
                repo=self.repo,
                runner=runner,
                illm_state_dir=state_dir,
            )

        self.assertEqual(verdict["verdict"], "merged")
        self.assertEqual(sleeps, [1.0, 1.0, 1.0])
        self.assertEqual(status_calls, [0.0])

    def test_poll_ci_notice_queries_status_again_only_after_status_interval(self) -> None:
        state_dir = self.root / "illm-empty"
        clock = [0.0]
        status_calls: list[float] = []

        def fake_monotonic() -> float:
            return clock[0]

        def fake_sleep(seconds: float) -> None:
            clock[0] += seconds

        def runner(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            status_calls.append(clock[0])
            if len(status_calls) == 1:
                return _completed(argv, stdout=json.dumps({"status": "running", "head_sha": "cafe123456"}))
            return _completed(argv, stdout=json.dumps({"verdict": "green", "head_sha": "cafe123456"}))

        with mock.patch.object(ceremony.time, "monotonic", side_effect=fake_monotonic), \
                mock.patch.object(ceremony.time, "sleep", side_effect=fake_sleep):
            verdict = poll_ci_notice(
                "D05",
                88,
                "cafe123456",
                0,
                run_dir=self.run_dir,
                repo=self.repo,
                runner=runner,
                illm_state_dir=state_dir,
                poll_interval=0.05,
                status_interval=0.2,
                timeout=2.0,
            )

        self.assertEqual(verdict["verdict"], "green")
        self.assertEqual(len(status_calls), 2)
        self.assertGreaterEqual(status_calls[1] - status_calls[0], 0.2 - 1e-9)


if __name__ == "__main__":
    unittest.main()
