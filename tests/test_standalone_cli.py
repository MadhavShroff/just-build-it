"""Offline tests for the `fleet` and `send` CLI entry points. No live service, no provider call."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jbi import cli
from jbi.tracking import Tracking

SHA = "a" * 40


def run_cli(*argv: str):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(list(argv))
        except SystemExit as exit_:
            rc = exit_.code
    return rc, out.getvalue(), err.getvalue()


class FakeClient:
    def __init__(self):
        self.sent = []

    def send(self, ctype, payload=None):
        self.sent.append((ctype, payload))
        return {"ok": True}

    def fleet(self):
        return {"sessions": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-cli-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.fake = FakeClient()
        patcher = mock.patch.object(cli, "run_client", return_value=self.fake)
        self.run_client = patcher.start()
        self.addCleanup(patcher.stop)

    def store(self) -> Tracking:
        tr = Tracking(self.tmp / "fleet.db")
        self.addCleanup(tr.close)
        return tr


class FleetOffline(Base):
    def test_missing_store_is_not_created(self):
        rc, out, err = run_cli("fleet", "--offline", "--run-dir", str(self.tmp))
        self.assertNotEqual(rc, 0)
        self.assertFalse((self.tmp / "fleet.db").exists())
        self.run_client.assert_not_called()

    def test_empty_store_prints_null_cache_inputs(self):
        self.store().close()
        rc, out, err = run_cli("fleet", "--offline", "--run-dir", str(self.tmp))
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        for key in ("sessions", "tasks", "dispatches", "leases", "ci_submissions", "merges",
                    "messages", "costs"):
            self.assertEqual(data[key], [], key)
        for key in ("cache_read", "cache_write", "uncached_input", "cache_hit_rate"):
            self.assertIsNone(data[key], key)
        self.run_client.assert_not_called()

    def test_cost_inputs_stay_distinct(self):
        tr = self.store()
        tr.create_task(title="t", owns=["src/a"], branch="b", worktree=self.tmp / "w",
                       base_commit=SHA, task_id="t1")
        d = tr.dispatch("t1", session_id="s1", owns=["src/a"], branch="b", worktree=self.tmp / "w")
        tr.record_cost(d["dispatch_id"], input_tokens=10, cache_read=30, cache_write=60)
        rc, out, err = run_cli("fleet", "--offline", "--run-dir", str(self.tmp))
        data = json.loads(out)
        self.assertEqual((data["uncached_input"], data["cache_read"], data["cache_write"]), (10, 30, 60))
        self.assertAlmostEqual(data["cache_hit_rate"], 0.3)
        self.assertEqual([s["id"] for s in data["sessions"]], ["s1"])
        self.assertEqual(len(data["leases"]), 1)

    def test_unknown_cost_is_not_zero(self):
        tr = self.store()
        tr.create_task(title="t", owns=["src/a"], branch="b", worktree=self.tmp / "w",
                       base_commit=SHA, task_id="t1")
        d = tr.dispatch("t1", session_id="s1", owns=["src/a"], branch="b", worktree=self.tmp / "w")
        tr.record_cost(d["dispatch_id"], output_tokens=5)
        data = json.loads(run_cli("fleet", "--offline", "--run-dir", str(self.tmp))[1])
        self.assertIsNone(data["cache_read"])
        self.assertIsNone(data["cache_hit_rate"])
        self.assertIsNone(data["costs"][0]["input_tokens"])


class SendRefusals(Base):
    def assertRefused(self, *argv):
        rc, out, err = run_cli("send", *argv)
        self.assertEqual(rc, 1, (out, err))
        self.assertEqual(json.loads(err)["error"]["code"], "payload_invalid")
        self.assertEqual(self.fake.sent, [])
        self.run_client.assert_not_called()

    def test_mixed_flags(self):
        self.assertRefused("--agent", "s1", "--all", "--text", "hi")

    def test_agent_all_with_all_flag(self):
        self.assertRefused("--agent", "all", "--all", "--text", "hi")

    def test_empty_and_blank_text(self):
        self.assertRefused("--agent", "s1")
        self.assertRefused("--agent", "s1", "--text", "   ")
        self.assertRefused("--all", "--text", "")

    def test_protocol_type_with_target(self):
        self.assertRefused("run.stop", "--agent", "s1", "--text", "hi")

    def test_text_without_target(self):
        self.assertRefused("--text", "hi")

    def test_no_type_no_target(self):
        self.assertRefused()

    def test_bad_recipient_id(self):
        self.assertRefused("--agent", "bad id!", "--text", "hi")
        self.assertRefused("--agent", "", "--text", "hi")


class SendTransport(Base):
    def test_agent_message(self):
        rc, out, err = run_cli("send", "--agent", "s1", "--text", "hello")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.fake.sent, [("message.send", {"target": "s1", "text": "hello",
                                                             "all_sessions": False})])

    def test_all_sessions_message(self):
        rc, out, err = run_cli("send", "--all", "--text", "hello")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.fake.sent, [("message.send", {"target": "all", "text": "hello",
                                                             "all_sessions": True})])

    def test_no_server_exits_nonzero(self):
        self.run_client.side_effect = SystemExit("No running dashboard")
        rc, out, err = run_cli("send", "--agent", "s1", "--text", "hi")
        self.assertNotEqual(rc, 0)

    def test_protocol_command_still_sends(self):
        rc, out, err = run_cli("send", "run.stop", '{"mode": "graceful"}')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.fake.sent, [("run.stop", {"mode": "graceful"})])

    def test_bad_json_payload(self):
        rc, out, err = run_cli("send", "run.stop", "{bad")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(err)["error"]["code"], "payload_invalid")


if __name__ == "__main__":
    unittest.main()
