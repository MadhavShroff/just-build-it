"""The message.send command, its engine delegation, and the authenticated fleet route."""

from __future__ import annotations

import http.cookiejar
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from jbi import protocol
from jbi.engine import Engine
from jbi.protocol import ProtocolError
from jbi.server import App, Auth, Server, fleet_snapshot
from jbi.store import Store, now
from jbi.tracking import Tracking


def new_store(directory: Path) -> Store:
    state = protocol.initial_state("r-msg", now(), name="demo", mode="new", target="",
                                   max_sessions=1, lanes=1, tmux="tmux")
    return Store(directory / "run.db", state)


class FakeRuntime:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def send_message(self, target, text, all_sessions=False) -> None:
        self.sent.append((target, text, all_sessions))


class MessageCommandTest(unittest.TestCase):
    def test_exact_fields(self) -> None:
        command = protocol.COMMANDS["message.send"]
        self.assertEqual(command.phases, protocol.ANY_PHASE)
        self.assertEqual(set(command.fields), {"target", "text", "all_sessions"})
        self.assertEqual((command.fields["target"].kind, command.fields["target"].max_length, command.fields["target"].required),
                         ("string", 64, True))
        self.assertEqual((command.fields["text"].kind, command.fields["text"].max_length, command.fields["text"].required),
                         ("string", 20000, True))
        self.assertEqual((command.fields["all_sessions"].kind, command.fields["all_sessions"].required),
                         ("bool", False))

    def test_validation(self) -> None:
        ok = protocol.validate_command({"id": "m1", "type": "message.send",
                                        "payload": {"target": "s1", "text": "hi", "all_sessions": True}})
        self.assertEqual(ok[2], {"target": "s1", "text": "hi", "all_sessions": True})
        bad = [{"text": "hi"}, {"target": "s1"}, {"target": "x" * 65, "text": "hi"},
               {"target": "s1", "text": "x" * 20001}, {"target": "s1", "text": "hi", "all_sessions": "yes"},
               {"target": "s1", "text": "hi", "extra": 1}, {"target": "s1", "text": "  "}]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                protocol.validate_command({"id": "m2", "type": "message.send", "payload": payload})
        edge = protocol.validate_command({"id": "m3", "type": "message.send",
                                          "payload": {"target": "x" * 64, "text": "y" * 20000}})
        self.assertEqual(len(edge[2]["text"]), 20000)

    def test_catalog_lists_the_command(self) -> None:
        self.assertIn("message.send", protocol.catalog()["commands"])


class EngineDelegationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = new_store(Path(self.tmp.name))
        self.addCleanup(self.store.close)

    def test_every_command_has_a_handler(self) -> None:
        engine = Engine(self.store, None, None)
        self.assertEqual(set(protocol.COMMANDS) - set(engine._handlers), set())
        self.assertIn("message.send", engine._handlers)

    def test_engine_delegates_to_runtime(self) -> None:
        runtime = FakeRuntime()
        engine = Engine(self.store, None, runtime)
        engine.submit({"id": "m1", "type": "message.send", "payload": {"target": "s1", "text": "go"}})
        engine.submit({"id": "m2", "type": "message.send",
                       "payload": {"target": "all", "text": "stop", "all_sessions": True}})
        self.assertEqual(runtime.sent, [("s1", "go", False), ("all", "stop", True)])

    def test_repeated_id_sends_once(self) -> None:
        runtime = FakeRuntime()
        engine = Engine(self.store, None, runtime)
        body = {"id": "m1", "type": "message.send", "payload": {"target": "s1", "text": "go"}}
        engine.submit(body)
        engine.submit(body)
        self.assertEqual(len(runtime.sent), 1)

    def test_no_runtime_is_a_conflict(self) -> None:
        engine = Engine(self.store, None, None)
        with self.assertRaises(ProtocolError) as caught:
            engine.submit({"id": "m1", "type": "message.send", "payload": {"target": "s1", "text": "go"}})
        self.assertEqual(caught.exception.code, "conflict")


class FleetRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = new_store(self.dir)
        self.addCleanup(self.store.close)
        self.auth = Auth()
        self.calls: list[int] = []
        self.app = App(self.store, Engine(self.store, None, None), self.auth)
        self.server = Server(self.app, ["127.0.0.1"]).start()
        self.addCleanup(self.server.stop)
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path: str, **headers):
        request = urllib.request.Request(self.base + path, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_fleet_route_is_not_public(self) -> None:
        status, body = self.call("/api/v1/fleet")
        self.assertEqual((status, body["error"]["code"]), (401, "unauthenticated"))
        from jbi import server
        import inspect
        self.assertNotIn("/api/v1/fleet", inspect.getsource(server.make_handler).split("PUBLIC = {")[1].split("}")[0])

    def test_fleet_route_serves_a_signed_in_session(self) -> None:
        request = urllib.request.Request(self.base + "/api/v1/session", method="POST",
                                         data=json.dumps({"key": self.auth.new_key()}).encode(),
                                         headers={"Content-Type": "application/json", "Origin": self.base})
        self.opener.open(request, timeout=5).read()
        status, body = self.call("/api/v1/fleet")
        self.assertEqual(status, 200)
        self.assertEqual(body["protocol"], protocol.PROTOCOL_VERSION)
        self.assertIn("run", body["fleet"], "without a fleet store the run state is served")

    def test_fleet_route_accepts_a_bearer_token(self) -> None:
        status, body = self.call("/api/v1/fleet", Authorization=f"Bearer {self.auth.token}")
        self.assertEqual(status, 200)
        self.assertIn("fleet", body)

    def test_fleet_snapshot_prefers_the_callback(self) -> None:
        self.app.fleet_reader = lambda: {"sessions": ["from-callback"]}
        self.assertEqual(fleet_snapshot(self.app), {"sessions": ["from-callback"]})

    def test_fleet_snapshot_reads_the_tracking_store(self) -> None:
        tracking = Tracking(self.dir / "fleet.db")
        tracking.close()
        snap = fleet_snapshot(self.app)
        for name in ("sessions", "tasks", "dispatches", "leases", "ci_submissions", "merges", "messages", "costs"):
            self.assertEqual(snap[name], [])
        self.assertIsNone(snap["cache_hit_rate"])


if __name__ == "__main__":
    unittest.main()
