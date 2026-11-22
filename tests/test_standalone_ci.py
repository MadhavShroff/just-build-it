"""CIAdapter: the direct service boundary, tested with fake runners. No live service."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from jbi import ci_adapter, lanes, worktrees as wt
from jbi.ci_adapter import CIAdapter, CIAdapterError
from jbi.tracking import Tracking

ROOT = Path(__file__).resolve().parents[1]
EXEC = "ci-conductor"


def done(out: str = "", code: int = 0, err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, out, err)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        wt.init_repo(self.repo)
        (self.repo / "a.txt").write_text("base\n")
        self.base = wt.commit_all(self.repo, "init")
        wt.git(self.repo, "checkout", "-q", "-b", "jbi/t1")
        (self.repo / "b.txt").write_text("work\n")
        self.head = wt.commit_all(self.repo, "work")
        wt.git(self.repo, "checkout", "-q", "main")
        self.task = {"id": "t1", "title": "T one", "goal": "Do it", "branch": "jbi/t1"}
        self.calls: list[list[str]] = []
        self.pr = 812
        self.reply: dict | str = None  # type: ignore[assignment]
        self.service_code = 0
        self.listed = "[]"

    def queued(self, **over) -> dict:
        reply = {"type": "queued", "job": "pr-812", "expect": "notice-on-completion",
                 "text": f"#{self.pr} at {self.head[:9]} is queued."}
        reply.update(over)
        return reply

    def runner(self, argv, cwd):
        self.calls.append(list(argv))
        if argv[0] == EXEC:
            body = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
            return done(body, self.service_code)
        if argv[0] == "git":
            return done()
        if argv[:3] == ["gh", "pr", "list"]:
            return done(self.listed)
        if argv[:3] == ["gh", "pr", "create"]:
            return done(f"https://example.test/pull/{self.pr}")
        return done()

    def adapter(self) -> CIAdapter:
        return CIAdapter(runner=self.runner)


class HandoverTest(Fixture):
    def test_ready_uses_direct_argv(self) -> None:
        self.reply = self.queued()
        result = self.adapter().handover(self.repo, self.task, verb="ready")
        self.assertIn([EXEC, "ready", "812", "--head", self.head], self.calls)
        self.assertEqual(result.pr_number, 812)
        self.assertEqual(result.head_sha, self.head)
        self.assertEqual(result.base_commit, self.base)

    def test_no_indirect_router_in_any_argv(self) -> None:
        self.reply = self.queued()
        self.adapter().handover(self.repo, self.task)
        for argv in self.calls:
            self.assertNotEqual(argv[0], "illm")
            self.assertNotIn("ci", argv[:2])
            if argv[0] == EXEC:
                self.assertNotIn("--json", argv)

    def test_push_and_draft_pr_only(self) -> None:
        self.reply = self.queued()
        self.adapter().handover(self.repo, self.task)
        tools = {(a[0], tuple(a[1:3]) if a[0] == "gh" else None) for a in self.calls}
        for argv in self.calls:
            if argv[0] == "gh":
                self.assertIn(argv[1:3], (["pr", "list"], ["pr", "create"]))
        self.assertTrue(any(a[:3] == ["gh", "pr", "create"] and "--draft" in a for a in self.calls))
        create = next(a for a in self.calls if a[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "main")
        self.assertTrue(tools)

    def test_ready_is_the_default_verb(self) -> None:
        self.reply = self.queued()
        self.adapter().handover(self.repo, self.task)
        self.assertTrue(any(a[:2] == [EXEC, "ready"] for a in self.calls))

    def test_submit_uses_its_real_argument_contract(self) -> None:
        self.reply = {"type": "queued", "job": "S12", "expect": "notice-on-completion",
                      "text": "stack S12: 1 of 1 queued.\n#812 queued at " + self.head[:9]}
        self.adapter().handover(self.repo, self.task, verb="submit")
        self.assertIn([EXEC, "submit", str(self.pr)], self.calls)

    def test_refused_reply_raises(self) -> None:
        self.reply = {"type": "refused", "reason": "shadow-mode", "text": "no"}
        self.service_code = 3
        with self.assertRaisesRegex(CIAdapterError, "refused: shadow-mode"):
            self.adapter().handover(self.repo, self.task, verb="ready")

    def test_failed_and_need_replies_raise(self) -> None:
        for reply in ({"type": "failed", "error": "boom"}, {"type": "need", "missing": "head"}):
            self.reply = reply
            with self.assertRaises(CIAdapterError):
                self.adapter().handover(self.repo, self.task)

    def test_missing_head_raises_before_any_push(self) -> None:
        self.task["branch"] = "jbi/none"
        self.reply = self.queued()
        with self.assertRaisesRegex(CIAdapterError, "no full 40-hex"):
            self.adapter().handover(self.repo, self.task)
        self.assertEqual(self.calls, [])

    def test_wrong_base_raises(self) -> None:
        self.reply = self.queued()
        self.listed = json.dumps([{"number": 812, "baseRefName": "develop"}])
        with self.assertRaisesRegex(CIAdapterError, "targets 'develop'"):
            self.adapter().handover(self.repo, self.task)

    def test_missing_base_commit_raises(self) -> None:
        self.reply = self.queued()
        with self.assertRaisesRegex(CIAdapterError, "base nope"):
            self.adapter().handover(self.repo, self.task, base="nope")

    def test_non_json_reply_raises(self) -> None:
        self.reply = "queued #812 ok"
        with self.assertRaisesRegex(CIAdapterError, "no JSON"):
            self.adapter().handover(self.repo, self.task)

    def test_unknown_type_raises(self) -> None:
        self.reply = {"type": "weird"}
        with self.assertRaisesRegex(CIAdapterError, "known type"):
            self.adapter().handover(self.repo, self.task)

    def test_missing_type_raises(self) -> None:
        self.reply = {"status": "queued"}
        with self.assertRaises(CIAdapterError):
            self.adapter().handover(self.repo, self.task)

    def test_non_queued_ok_reply_is_not_ready(self) -> None:
        self.reply = {"type": "ok"}
        with self.assertRaisesRegex(CIAdapterError, "did not queue"):
            self.adapter().handover(self.repo, self.task)

    def test_reply_naming_other_head_or_pr_is_rejected(self) -> None:
        for text in (f"#812 at {'0' * 9} is queued.", f"#999 at {self.head[:9]} is queued."):
            self.reply = self.queued(text=text)
            with self.assertRaisesRegex(CIAdapterError, "does not name"):
                self.adapter().handover(self.repo, self.task)

    def test_nonzero_exit_with_ok_type_raises(self) -> None:
        self.reply = self.queued()
        self.service_code = 1
        with self.assertRaisesRegex(CIAdapterError, "exited 1"):
            self.adapter().handover(self.repo, self.task)

    def test_tracking_records_bound_evidence(self) -> None:
        self.reply = self.queued()
        tracking = Tracking(self.root / "fleet.db")
        self.addCleanup(tracking.close)
        tracking.create_task(title="T one", owns=["b.txt"], branch="jbi/t1", worktree=self.repo,
                             base_commit=self.base, task_id="t1")
        result = self.adapter().handover(self.repo, self.task, tracking=tracking)
        rows = tracking.fleet()["ci_submissions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["pr_number"], rows[0]["head_sha"], rows[0]["base_commit"], rows[0]["state"]),
                         (812, self.head, self.base, "submitted"))
        self.assertEqual(rows[0]["service_reply"]["bound"]["task"], "t1")
        self.assertEqual(result.reply["bound"]["base_commit"], self.base)

    def test_executable_from_environment(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"JBI_CI_CONDUCTOR": "/opt/x/ci"}):
            self.assertEqual(CIAdapter().executable, "/opt/x/ci")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JBI_CI_CONDUCTOR", None)
            self.assertEqual(CIAdapter().executable, "ci-conductor")


class ConfirmMergeTest(Fixture):
    def result(self) -> ci_adapter.HandoverResult:
        self.reply = self.queued()
        return self.adapter().handover(self.repo, self.task)

    def job_text(self, verdict: str = "GREEN", merge: bool = True) -> dict:
        lines = [f"prs: #{self.pr}@{self.head[:9]}", f"verdict {verdict}  failed gates: none", "finished 1"]
        if merge:
            lines.append(f"merge: #{self.pr}->{self.base[:9]}; stopped=None")
        return {"type": "data", "job": "pr-812", "text": "\n".join(lines)}

    def test_running_job_is_pending_and_terminal_job_is_not(self):
        result = self.result()
        self.reply = {"type": "data", "job": "pr-812", "text": "started 1  finished running\n"}
        self.assertTrue(self.adapter().pending(self.repo, result))
        self.reply = self.job_text()
        self.assertFalse(self.adapter().pending(self.repo, result))

    def test_submit_reply_alone_proves_no_merge(self) -> None:
        result = self.result()
        self.reply = self.job_text(merge=False)
        with self.assertRaisesRegex(CIAdapterError, "no merge record"):
            self.adapter().confirm_merge(self.repo, result)

    def test_missing_green_evidence_raises(self) -> None:
        result = self.result()
        self.reply = self.job_text(verdict="RED")
        with self.assertRaisesRegex(CIAdapterError, "no green verdict"):
            self.adapter().confirm_merge(self.repo, result)

    def test_wrong_head_in_job_raises(self) -> None:
        result = self.result()
        reply = self.job_text()
        reply["text"] = reply["text"].replace(self.head[:9], "f" * 9)
        self.reply = reply
        with self.assertRaisesRegex(CIAdapterError, "does not name"):
            self.adapter().confirm_merge(self.repo, result)

    def test_green_merge_is_recorded(self) -> None:
        result = self.result()
        tracking = Tracking(self.root / "fleet.db")
        self.addCleanup(tracking.close)
        tracking.create_task(title="T one", owns=["b.txt"], branch="jbi/t1", worktree=self.repo,
                             base_commit=self.base, task_id="t1")
        self.reply = self.job_text()
        self.calls.clear()

        def runner(argv, cwd):
            if argv[0] == "git" and "rev-parse" in argv:
                self.calls.append(argv)
                return done(self.base)
            return self.runner(argv, cwd)

        evidence = CIAdapter(runner=runner).confirm_merge(self.repo, result, tracking=tracking)
        self.assertEqual(evidence["squash_sha"], self.base)
        fleet = tracking.fleet()
        self.assertEqual(len(fleet["merges"]), 1)
        self.assertEqual(fleet["tasks"][0]["state"], "merged")


class SourceGuardTest(unittest.TestCase):
    def test_no_local_landing_names(self) -> None:
        for name in ("lanes.py", "ci_adapter.py"):
            text = (ROOT / "jbi" / name).read_text()
            for banned in ("squash_merge_tree", "fast_forward_main", "commit_tree", "class Lanes"):
                self.assertNotIn(banned, text, f"{name} has {banned}")
        self.assertFalse(hasattr(lanes, "Lanes"))
        self.assertTrue(hasattr(lanes, "MergeJob"))

    def test_adapter_names_no_indirect_router(self) -> None:
        text = (ROOT / "jbi" / "ci_adapter.py").read_text().lower()
        for banned in ("illm", "interface-llm", "mailbox"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
