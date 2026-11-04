"""Core contract tests: JSON Patch, protocol validation, store, waves, engine, HTTP server."""

from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from jbi import cli, planner
from jbi import jsonpatch as jp
from jbi import protocol, rigor, waves
from jbi.engine import Engine
from jbi.llm import CompletionError
from jbi.protocol import ProtocolError
from jbi.server import MAX_BODY, App, Auth, Server
from jbi.store import Changes, Store, now

ROOT = Path(__file__).resolve().parent.parent


def new_store(directory: Path) -> Store:
    state = protocol.initial_state("r-test", now(), name="demo", mode="new", target="",
                                   max_sessions=3, lanes=2, tmux="tmux -L jbi-r-test attach")
    return Store(directory / "run.db", state)


def task(tid: str, *deps: str, owns: tuple[str, ...] = ()) -> dict:
    return {"id": tid, "title": tid.title(), "goal": f"Build {tid}", "deps": list(deps),
            "acceptance": "true", "owns": list(owns)}


class JsonPatchTest(unittest.TestCase):
    def test_add_replace_remove_and_escaping(self):
        doc = {"tasks": {}}
        jp.apply(doc, [jp.add(jp.pointer("tasks", "a/b~c"), {"n": 1}),
                       jp.replace(jp.pointer("tasks", "a/b~c", "n"), 2),
                       jp.add("/list", []), jp.add("/list/-", "x"), jp.add("/list/0", "w")])
        self.assertEqual(doc["tasks"]["a/b~c"]["n"], 2)
        self.assertEqual(doc["list"], ["w", "x"])
        jp.apply(doc, [jp.remove("/list/0")])
        self.assertEqual(doc["list"], ["x"])

    def test_bad_operations_raise(self):
        for op in (jp.replace("/missing", 1), jp.remove("/missing"), {"op": "move", "path": "/a"},
                   jp.add("/list/9", 1)):
            with self.subTest(op=op), self.assertRaises(jp.PatchError):
                jp.apply({"list": []}, [op])

    def test_failed_patch_leaves_store_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            seq = store.seq
            with self.assertRaises(jp.PatchError):
                store.commit("log.line", [jp.replace("/run/name", "x"), jp.replace("/nope", 1)])
            self.assertEqual((store.seq, store.read("/run/name")), (seq, "demo"))
            store.close()


class ProtocolTest(unittest.TestCase):
    def test_every_command_validates_its_payload(self):
        cid, ctype, payload = protocol.validate_command(
            {"id": "c-1", "type": "run.configure", "payload": {"rigor": "vibe", "profile": "micro"}})
        self.assertEqual((cid, ctype, payload), ("c-1", "run.configure", {"rigor": "vibe", "profile": "micro"}))
        cases = [({"id": "", "type": "run.start"}, "payload_invalid"),
                 ({"id": "c", "type": "nope"}, "command_unknown"),
                 ({"id": "c", "type": "run.configure", "payload": {"rigor": "vibe"}}, "payload_invalid"),
                 ({"id": "c", "type": "run.stop", "payload": {"mode": "explode"}}, "payload_invalid"),
                 ({"id": "c", "type": "run.start", "payload": {"extra": 1}}, "payload_invalid"),
                 ({"id": "c", "type": "task.retry", "payload": {"task": "Bad Id"}}, "payload_invalid")]
        for body, code in cases:
            with self.subTest(body=body), self.assertRaises(ProtocolError) as caught:
                protocol.validate_command(body)
            self.assertEqual(caught.exception.code, code)

    def test_secret_fields_are_redacted(self):
        redacted = protocol.redact("account.set", {"provider": "claude", "enabled": True, "api_key": "sk-1"})
        self.assertEqual(redacted["api_key"], "<redacted>")

    def test_catalog_is_complete_and_serializable(self):
        catalog = json.loads(json.dumps(protocol.catalog()))
        self.assertEqual(set(catalog["commands"]), set(protocol.COMMANDS))
        self.assertIn("api_key", catalog["commands"]["account.set"]["payload"])


class WavesTest(unittest.TestCase):
    def test_kahn_levels_in_plan_order(self):
        plan = waves.compile_plan([task("core"), task("api", "core"), task("ui", "core"),
                                   task("docs"), task("e2e", "api", "ui")], 1)
        self.assertEqual(plan["waves"], [["core", "docs"], ["api", "ui"], ["e2e"]])
        self.assertEqual(plan["tasks"]["api"]["status"], "blocked")
        self.assertEqual(plan["tasks"]["docs"]["status"], "ready")

    def test_cycle_is_named(self):
        with self.assertRaises(ProtocolError) as caught:
            waves.validate([task("docs"), task("a", "c"), task("b", "a"), task("c", "b")])
        self.assertEqual(caught.exception.details["cycle"], ["a", "c", "b", "a"])

    def test_invalid_plans(self):
        for tasks in ([], [task("a"), task("a")], [task("a", "x")], [task("a", "a")],
                      [task("a", owns=("../etc",))], [{**task("a"), "acceptance": " "}]):
            with self.subTest(tasks=tasks), self.assertRaises(ProtocolError):
                waves.validate(tasks)

    def test_a_ready_task_never_waits_for_its_wave(self):
        plan = waves.compile_plan([task("a"), task("slow"), task("b", "a")], 1)
        plan["tasks"]["a"]["status"] = "merged"
        plan["tasks"]["slow"]["status"] = "running"
        self.assertEqual(waves.ready_tasks(plan), ["b"])

    def test_owns_conflict(self):
        plan = waves.compile_plan([task("a", owns=("src/x.py",)), task("b", owns=("src/x.py",)),
                                   task("c", owns=("src/y.py",))], 1)
        self.assertEqual(waves.owns_conflict(plan, "b", ["a"]), "a")
        self.assertIsNone(waves.owns_conflict(plan, "c", ["a"]))

    def test_deep_plan_has_no_recursion_limit(self):
        chain = [task("t0")] + [task(f"t{i}", f"t{i - 1}") for i in range(1, 1500)]
        self.assertEqual(len(waves.compile_plan(chain, 1)["waves"]), 1500)


class RigorTest(unittest.TestCase):
    def test_vibe_has_no_checks_and_xhigh_has_two_reviews(self):
        self.assertEqual([s.role for s in rigor.pipeline("vibe")], ["implement", "merge"])
        self.assertEqual([s.role for s in rigor.pipeline("xhigh")].count("review"), 2)
        self.assertEqual(rigor.LEVELS["vibe"]["lane_checks"], ())

    def test_escalation_moves_the_implementer_up(self):
        self.assertEqual(rigor.tier_for("economy", "implement", 0), "S")
        self.assertEqual(rigor.tier_for("economy", "implement", 2), "L")
        self.assertEqual(rigor.tier_for("micro", "verify", 9), "XS")

    def test_task_override_wins(self):
        config = {"rigor": "high", "profile": "balanced"}
        self.assertEqual(rigor.effective({"override": {"rigor": "vibe"}}, config), ("vibe", "balanced"))


class StoreTest(unittest.TestCase):
    def test_replay_after_restart_and_timeline_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            for i in range(250):
                store.log("test", f"line {i}")
            seq = store.seq
            store.close()
            again = Store(Path(tmp) / "run.db")
            self.assertEqual(again.seq, seq)
            timeline = again.read("/timeline")
            self.assertEqual(len(timeline), 200)
            self.assertEqual(timeline[-1]["text"], "line 249")
            again.close()

    def test_wait_wakes_on_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            start = store.seq
            import threading
            threading.Timer(0.1, lambda: store.commit("log.line", Changes().replace("run", "name", "x"))).start()
            self.assertGreater(store.wait(start, 2.0), start)
            store.close()


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = new_store(Path(self.tmp.name))
        self.engine = Engine(self.store, completer=None, runtime=None)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def send(self, ctype, payload=None, cid=None):
        return self.engine.submit({"id": cid or f"c-{time.time_ns()}", "type": ctype, "payload": payload or {}})

    def test_phase_guard_and_idempotency(self):
        with self.assertRaises(ProtocolError) as caught:
            self.send("run.start")
        self.assertEqual(caught.exception.code, "phase_invalid")
        first = self.send("intake.detail", {"level": "delegate"}, cid="same")
        second = self.send("intake.detail", {"level": "user"}, cid="same")
        self.assertEqual(first, second)
        self.assertEqual(self.store.read("/intake/detail"), "delegate")

    def test_finish_needs_input(self):
        with self.assertRaises(ProtocolError) as caught:
            self.send("intake.finish")
        self.assertEqual(caught.exception.code, "conflict")

    def test_stop_without_runtime_completes(self):
        self.send("run.stop", {"mode": "quit"})
        self.assertEqual(self.store.read("/run/stopping/mode"), "quit")

    # finding 7: one lock around lookup, phase check, handler and record
    def test_concurrent_duplicate_id_runs_the_handler_once(self):
        calls = []

        def counting(*args):
            calls.append(args)
            time.sleep(0.02)                    # widen the window between lookup and record
        self.engine._handlers["intake.detail"] = counting
        barrier, results = threading.Barrier(20), []

        def post():
            barrier.wait()
            results.append(self.send("intake.detail", {"level": "user"}, cid="dup"))
        threads = [threading.Thread(target=post) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 20)
        self.assertEqual(len({json.dumps(r, sort_keys=True) for r in results}), 1)

    def test_task_control_gets_its_own_command_type(self):
        actions = []

        class Runtime:
            def control(self, task, action):
                time.sleep(0.02)
                actions.append((task, action))
        self.engine.runtime = Runtime()
        self.store.commit("plan.updated", Changes().replace("plan", waves.compile_plan([task("core")], 1))
                          .replace("run", "phase", "execute"))
        threads = [threading.Thread(target=self.send, args=(ctype, {"task": "core"}))
                   for ctype in ("task.retry", "task.pause", "task.resume")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(actions), [("core", "pause"), ("core", "resume"), ("core", "retry")])
        self.assertFalse(hasattr(self.engine, "_current_type"))

    # finding 12 (engine side): bootstrap.retry and stack.back from a failed bootstrap
    def test_failed_bootstrap_can_retry_or_go_back(self):
        retries = []

        class Runtime:
            def retry_bootstrap(self):
                retries.append(True)
        self.engine.runtime = Runtime()
        self.store.commit("bootstrap.status", Changes().replace("run", "phase", "bootstrap")
                          .replace("bootstrap", "status", "running").replace("stack", "accepted", True))
        for ctype in ("bootstrap.retry", "stack.back"):
            with self.subTest(ctype=ctype), self.assertRaises(ProtocolError) as caught:
                self.send(ctype)
            self.assertEqual(caught.exception.code, "conflict")
        self.store.commit("bootstrap.status", Changes().replace("bootstrap", "status", "failed"))
        self.send("bootstrap.retry")
        self.assertEqual((retries, self.store.read("/bootstrap/status")), ([True], "running"))
        self.store.commit("bootstrap.status", Changes().replace("bootstrap", "status", "failed"))
        self.send("stack.back")
        self.assertEqual((self.store.read("/run/phase"), self.store.read("/stack/accepted"),
                          self.store.read("/bootstrap/status")), ("stack", False, "pending"))
        self.send("stack.back")
        self.assertEqual(self.store.read("/run/phase"), "intake")

    # finding 10 (engine side): a failed model step logs a fixed sentence, never provider text
    def test_background_failure_keeps_provider_text_out_of_the_log(self):
        leak = "sk-live-0123456789abcdef0123456789abcdef"

        def failing(prompt):
            raise ValueError(f"exit 1: Authorization: Bearer {leak}")
        self.engine.completer = failing
        self.send("intake.say", {"text": "a todo app"})
        deadline = time.monotonic() + 5
        while self.store.read("/run/busy") is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        events = json.dumps(self.store.events_after(0, 10000))
        self.assertNotIn(leak, events)
        self.assertIn("Thinking failed (ValueError)", events)
        log = Path(self.tmp.name) / "debug.log"
        self.assertEqual(os.stat(log).st_mode & 0o777, 0o600)
        self.assertNotIn(leak, log.read_text())
        self.assertIn("<redacted>", log.read_text())

    def test_completion_error_text_reaches_the_timeline(self):
        def failing(prompt):
            raise CompletionError("No enabled provider answered. claude: exit 2. The CLI call failed.")
        self.engine.completer = failing
        self.send("intake.say", {"text": "x"})
        deadline = time.monotonic() + 5
        while self.store.read("/run/busy") is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("claude: exit 2. The CLI call failed.", self.store.read("/timeline")[-1]["text"])


class SignalTest(unittest.TestCase):
    """Finding 9: the first SIGINT or SIGTERM stands down; any later one kills at once."""

    def fake_run(self):
        run = mock.Mock()
        run.stopped, run.stop_requested = threading.Event(), threading.Event()
        return run

    def test_second_signal_kills_during_standdown(self):
        run, kills = self.fake_run(), []
        handler = cli.signal_handler(run, kill=kills.append)
        with mock.patch("builtins.print"):
            handler(signal.SIGINT, None)
            self.assertTrue(run.stop_requested.is_set())
            self.assertEqual(kills, [])
            handler(signal.SIGTERM, None)       # standdown is still running: run.stopped is not set
        self.assertEqual(kills, [run])
        deadline = time.monotonic() + 2
        while run.engine.submit.call_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(run.engine.submit.call_count, 1)
        self.assertEqual(run.engine.submit.call_args[0][0]["payload"], {"mode": "standdown"})

    def test_hard_kill_order(self):
        run, order = self.fake_run(), []
        run.run_id = "r-x"
        run.runtime.stop.side_effect = lambda mode: order.append(("stop", mode))
        with mock.patch.object(cli.subprocess, "run", side_effect=lambda argv, **kw: order.append(("tmux", argv))), \
                mock.patch.object(cli.os, "kill", side_effect=lambda pid, sig: order.append(("kill", pid, sig))):
            cli.hard_kill(run)
        self.assertEqual(order, [("stop", "kill"), ("tmux", ["tmux", "-L", "jbi-r-x", "kill-server"]),
                                 ("kill", os.getpid(), signal.SIGKILL)])


class DocsAndSlugTest(unittest.TestCase):
    """Findings 12 and 14: the protocol document matches the code; one slug rule everywhere."""

    def test_protocol_doc_lists_every_code_command_status_and_field(self):
        doc = (ROOT / "docs" / "PROTOCOL.md").read_text()
        for code, status in ProtocolError.STATUS.items():
            with self.subTest(code=code):
                self.assertRegex(doc, rf"\| {status} \| `{code}` \|")
        for ctype in protocol.COMMANDS:
            self.assertIn(f"| `{ctype}` |", doc)
        for status in protocol.TASK_STATUSES:
            self.assertIn(f"`{status}`", doc)
        for name in ('"info"', '"busy"', "version", "`run.busy`"):
            self.assertIn(name, doc)
        self.assertIn("bootstrap.retry", protocol.catalog()["commands"])
        self.assertIn("skipped", protocol.catalog()["task_statuses"])
        self.assertEqual(protocol.COMMANDS["stack.back"].phases, ("stack", "bootstrap"))

    def test_cli_uses_the_planner_slug(self):
        self.assertIs(cli.slug, planner.slug)

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_dashboard_slug_matches_the_planner_slug(self):
        samples = ["My App", "Café Über", "  --9 lives--  ", "123", "", "a" * 60, "x" + "-" * 39 + "y",
                   "Hello, World! 2026", "ÀÉÎ", "a__b..c", "-start-", "9to5 Tracker", "tab\tnew\nline"]
        script = ("import { pathToFileURL } from 'node:url';"
                  "const m = await import(pathToFileURL(process.argv[1]).href);"
                  "console.log(JSON.stringify(JSON.parse(process.argv[2]).map((t) => m.slug(t))));")
        done = subprocess.run(["node", "--input-type=module", "-e", script, str(ROOT / "web" / "js" / "dom.js"),
                               json.dumps(samples)], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [planner.slug(t, "app") for t in samples])


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = new_store(Path(self.tmp.name))
        self.auth = Auth()
        app = App(self.store, Engine(self.store, None, None), self.auth,
                  link_bases=lambda: [{"label": "Local", "url": self.base}])
        self.server = Server(app, ["127.0.0.1"]).start()
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.server.stop()
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, **headers):
        headers = {"Content-Type": "application/json", "Origin": self.base, **headers}
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def sign_in(self):
        status, _ = self.call("POST", "/api/v1/session", {"key": self.auth.new_key()})
        self.assertEqual(status, 200)

    def test_health_is_public_and_state_is_not(self):
        self.assertEqual(self.call("GET", "/api/v1/health")[1]["run_id"], "r-test")
        status, body = self.call("GET", "/api/v1/state")
        self.assertEqual((status, body["error"]["code"]), (401, "unauthenticated"))

    def test_a_key_works_once(self):
        key = self.auth.new_key()
        self.assertEqual(self.call("POST", "/api/v1/session", {"key": key})[0], 200)
        self.assertEqual(self.call("POST", "/api/v1/session", {"key": key})[0], 401)

    def test_writes_need_a_matching_origin_and_json(self):
        self.sign_in()
        body = {"id": "c1", "type": "intake.detail", "payload": {"level": "user"}}
        self.assertEqual(self.call("POST", "/api/v1/commands", body, Origin="http://evil.test")[0], 403)
        self.assertEqual(self.call("POST", "/api/v1/commands", body, **{"Content-Type": "text/plain"})[0], 415)
        status, reply = self.call("POST", "/api/v1/commands", body)
        self.assertEqual((status, reply["accepted"]), (202, True))

    def test_bearer_token_for_machine_clients(self):
        status, body = self.call("GET", "/api/v1/state", Authorization=f"Bearer {self.auth.token}")
        self.assertEqual((status, body["protocol"]), (200, 1))

    def test_only_the_bearer_token_creates_sign_in_links(self):
        self.sign_in()
        self.assertEqual(self.call("POST", "/api/v1/links", {})[0], 401)        # a browser session cannot
        status, body = self.call("POST", "/api/v1/links", {}, Authorization=f"Bearer {self.auth.token}")
        self.assertEqual(status, 200)
        link = body["links"][0]
        self.assertEqual(link["label"], "Local")
        key = link["url"].split("#k=", 1)[1]
        self.assertEqual(self.call("POST", "/api/v1/session", {"key": key})[0], 200)
        self.assertEqual(self.call("POST", "/api/v1/session", {"key": key})[0], 401)

    def test_protocol_version_negotiation(self):
        self.assertEqual(self.call("GET", "/api/v1/health", **{"Accept-Protocol": "2"})[0], 406)

    def test_event_page_and_stream_resume(self):
        self.sign_in()
        for level in ("user", "guided"):
            self.call("POST", "/api/v1/commands", {"id": level, "type": "intake.detail", "payload": {"level": level}})
        page = self.call("GET", "/api/v1/events.json?after=1&limit=1")[1]
        self.assertEqual([e["seq"] for e in page["events"]], [2])
        self.assertTrue(page["more"])
        request = urllib.request.Request(self.base + "/api/v1/events", headers={"Last-Event-ID": "2"})
        with self.opener.open(request, timeout=5) as stream:
            lines = [stream.readline().decode().strip() for _ in range(5)]
        self.assertIn("id: 3", lines)
        self.assertIn("event: patch", lines)

    # finding 4: Content-Length must be an integer from 0 to MAX_BODY
    def raw_post(self, length_header: str, body: bytes = b"") -> tuple[int, dict, float]:
        start = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=3) as conn:
            conn.sendall(b"POST /api/v1/session HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                         + length_header.encode() + b"\r\n" + body)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            length = int(next(l.split(b":")[1] for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")))
            while len(rest) < length:
                rest += conn.recv(65536)
        return int(head.split()[1]), json.loads(rest[:length]), time.monotonic() - start

    def test_bad_content_length_is_refused_quickly(self):
        for header, code, status in (("Content-Length: -1\r\n", "payload_invalid", 400),
                                     ("Content-Length: abc\r\n", "payload_invalid", 400),
                                     ("Content-Length: 1e3\r\n", "payload_invalid", 400),
                                     (f"Content-Length: {MAX_BODY + 1}\r\n", "too_large", 413),
                                     ("Content-Length: 99999999999999999999\r\n", "too_large", 413)):
            with self.subTest(header=header):
                got, body, took = self.raw_post(header, b"a" * 1024)
                self.assertEqual((got, body["error"]["code"]), (status, code))
                self.assertLess(took, 2.0)
        got, body, _ = self.raw_post("")      # a missing header means an empty body
        self.assertEqual((got, body["error"]["code"]), (401, "unauthenticated"))

    def test_unknown_api_path_and_static_traversal(self):
        self.sign_in()
        self.assertEqual(self.call("GET", "/api/v1/nope")[0], 404)
        request = urllib.request.Request(self.base + "/../../etc/passwd")
        try:
            with self.opener.open(request, timeout=5) as response:
                body = response.read()
        except urllib.error.HTTPError as error:   # no dashboard installed: a clean 404
            self.assertEqual(error.code, 404)
            body = error.read()
        self.assertNotIn(b"root:", body)


if __name__ == "__main__":
    unittest.main()
