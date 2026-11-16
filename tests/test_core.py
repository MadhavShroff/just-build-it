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

from jbi import cli, dag_loader, planner
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

    def test_critical_path_and_describe_preview_etas_and_live_updates(self):
        t_core = {**task("core", owns=("src/core.py",)), "eta_s": 100}
        t_docs = {**task("docs", owns=("docs/readme.md",)), "eta_s": 40}
        t_api = {**task("api", "core", owns=("src/api.py", "src/routes.py")), "eta_s": 200}
        t_ui = {**task("ui", "core", owns=("web/ui.js",)), "eta_s": 80}
        t_e2e = {**task("e2e", "api", "ui", owns=("tests/e2e.py",)), "eta_s": 120}
        plan = waves.compile_plan([t_core, t_docs, t_api, t_ui, t_e2e], 1)
        for raw in (t_core, t_docs, t_api, t_ui, t_e2e):
            plan["tasks"][raw["id"]]["eta_s"] = raw["eta_s"]

        preview = waves.describe_preview(plan, {"rigor": "high", "max_sessions": 3, "dry_run": True})
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["total_tasks"], 5)
        self.assertEqual(preview["waves_total"], 3)
        self.assertEqual(preview["waves_completed"], 0)
        self.assertEqual(preview["critical_path"], ["core", "api", "e2e"])
        self.assertEqual(preview["critical_path_eta_s"], 420)
        self.assertEqual(preview["remaining_eta_s"], 420)
        self.assertTrue(preview["waves"][0]["parallel"])
        self.assertEqual(preview["waves"][0]["eta_s"], 100)
        self.assertEqual(preview["waves"][1]["eta_s"], 200)
        self.assertEqual(preview["waves"][0]["tasks"][0]["parallel_with"], ["docs"])

        plan["tasks"]["core"]["status"] = "merged"
        plan["tasks"]["docs"]["status"] = "merged"
        plan["tasks"]["api"]["status"] = "running"
        plan["tasks"]["ui"]["status"] = "ready"
        updated = waves.describe_preview(plan, {"rigor": "high", "max_sessions": 3, "dry_run": True})
        self.assertEqual(updated["waves_completed"], 1)
        self.assertEqual(updated["merged_tasks"], 2)
        self.assertEqual(updated["running_tasks"], 1)
        self.assertEqual(updated["waves"][0]["status"], "merged")
        self.assertEqual(updated["waves"][0]["remaining_eta_s"], 0)
        self.assertEqual(updated["waves"][1]["status"], "running")
        self.assertEqual(updated["remaining_eta_s"], 320)


class DagLoaderTest(unittest.TestCase):
    YAML_SAMPLE = (
        'project: "demo"\n'
        'repo: "./demo"\n'
        'mode: "existing"\n'
        'waves: "auto"\n'
        'tasks:\n'
        '  - id: "D01"\n'
        '    title: "Build core"\n'
        '    goal: "Provide core"\n'
        '    deps: []\n'
        '    acceptance: "python3 -m unittest"\n'
        '    owns: ["src/core.py"]\n'
        '  - id: "cli_1"\n'
        '    title: "Build CLI"\n'
        '    goal: "Expose core"\n'
        '    deps: ["D01"]\n'
        '    acceptance: "python3 -m unittest"\n'
        '    owns: ["src/cli.py"]\n'
    )

    def test_loads_restricted_yaml_and_explicit_waves(self):
        plan = dag_loader.load(self.YAML_SAMPLE, revision=2)
        self.assertEqual(plan["revision"], 2)
        self.assertEqual(plan["order"], ["D01", "cli_1"])
        self.assertEqual(plan["waves"], [["D01"], ["cli_1"]])
        self.assertEqual(plan["tasks"]["D01"]["status"], "ready")
        self.assertEqual(plan["tasks"]["cli_1"]["status"], "blocked")
        explicit = (
            'project: "demo"\nrepo: "./demo"\nmode: "new"\nwaves: "explicit"\n'
            'wave_groups: [["D01"], ["docs"], ["cli_1"]]\n'
            'tasks:\n'
            '  - id: "D01"\n    title: "Core"\n    goal: "G"\n    deps: []\n    acceptance: "true"\n'
            '  - id: "docs"\n    title: "Docs"\n    goal: "G"\n    deps: []\n    acceptance: "true"\n'
            '  - id: "cli_1"\n    title: "CLI"\n    goal: "G"\n    deps: ["D01"]\n    acceptance: "true"\n'
        )
        plan2 = dag_loader.load(explicit)
        self.assertEqual(plan2["waves"], [["D01"], ["docs"], ["cli_1"]])
        self.assertEqual(plan2["tasks"]["docs"]["wave"], 2)
        self.assertEqual(plan2["tasks"]["cli_1"]["wave"], 3)

    def test_loads_planning_dag_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dag_path = Path(tmp) / "dag.json"
            dag_path.write_text(json.dumps({
                "schemaVersion": 1,
                "nodes": [
                    {"id": "D01", "title": "Native data contracts", "deps": [],
                     "path": "04/D01-PLAN.md", "spec": "04/D01-SPEC.md", "owns": ["src/d01.py"]},
                    {"id": "A01", "title": "Account authority", "deps": [],
                     "path": "01/A01-PLAN.md", "owns": ["src/a01.py"]},
                    {"id": "P01", "title": "Project authority", "deps": ["D01", "A01"],
                     "path": "08/P01-PLAN.md", "acceptance": "pytest", "owns": ["src/p01.py"]},
                ],
                "waves": [["D01", "A01"], ["P01"]],
            }))
            plan = dag_loader.load(dag_path)
            self.assertEqual(plan["order"], ["D01", "A01", "P01"])
            self.assertEqual(plan["waves"], [["D01", "A01"], ["P01"]])
            self.assertEqual(plan["tasks"]["D01"]["status"], "ready")
            self.assertEqual(plan["tasks"]["P01"]["status"], "blocked")
            self.assertEqual(plan["tasks"]["P01"]["acceptance"], "pytest")
            self.assertIn("04/D01-PLAN.md", plan["tasks"]["D01"]["goal"])

    def test_rejects_cycles_and_malformed_yaml(self):
        cycle_yaml = (
            'project: "t"\nrepo: "./t"\nmode: "new"\nwaves: "auto"\ntasks:\n'
            '  - id: "a"\n    title: "A"\n    goal: "G"\n    deps: ["b"]\n    acceptance: "true"\n'
            '  - id: "b"\n    title: "B"\n    goal: "G"\n    deps: ["a"]\n    acceptance: "true"\n'
        )
        with self.assertRaises(dag_loader.Invalid) as caught:
            dag_loader.load(cycle_yaml)
        self.assertIn("a -> b -> a", str(caught.exception))
        with self.assertRaises(dag_loader.Invalid):
            dag_loader.load(self.YAML_SAMPLE + "# comment\n")

    def test_engine_plan_load_and_cli_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            dag_path = Path(tmp) / "sample.yaml"
            dag_path.write_text(self.YAML_SAMPLE)
            store = new_store(Path(tmp))
            started = []

            class FakeRuntime:
                def start_execution(self):
                    started.append(True)

            engine = Engine(store, completer=None, runtime=FakeRuntime())
            engine.submit({"id": "load-1", "type": "plan.load", "payload": {"path": str(dag_path)}})
            self.assertEqual(store.read("/run/phase"), "execute")
            self.assertEqual(store.read("/plan/order"), ["D01", "cli_1"])
            self.assertEqual(started, [True])
            store.close()
            args = cli.parse_args(["--dag", str(dag_path), "--start"])
            self.assertEqual((args.dag, args.start), (dag_path, True))
            preview_args = cli.parse_args(["--dag", str(dag_path), "--dry-run", "--preview"])
            self.assertEqual((preview_args.dag, preview_args.dry_run, preview_args.preview),
                             (dag_path, True, True))


class RigorTest(unittest.TestCase):
    def test_vibe_has_no_checks_and_xhigh_has_four_reviews(self):
        self.assertEqual([s.role for s in rigor.pipeline("vibe")], ["implement", "merge"])
        # FOUNDER RULE 2026-11-16: every review step runs four reviews, one on the
        # cascade-active model and three independent DeepSeek agents, plus pre-merge
        # test-audit, methodology-audit, and dry-run stages on high and xhigh.
        self.assertEqual([s.name for s in rigor.pipeline("medium") if s.role == "review"],
                         ["review-cascade", "review-ds-1", "review-ds-2", "review-ds-3"])
        for level in ("high", "xhigh"):
            self.assertEqual(
                [s.name for s in rigor.pipeline(level) if s.role == "review"],
                [
                    "review-cascade", "review-ds-1", "review-ds-2", "review-ds-3",
                    "test-audit", "methodology-audit", "dry-run",
                ],
            )
            self.assertEqual([s.name for s in rigor.pipeline(level)[-4:]],
                             ["test-audit", "methodology-audit", "dry-run", "merge"])
        self.assertEqual(rigor.LEVELS["vibe"]["lane_checks"], ())

    def test_pre_merge_audit_stage_briefs(self):
        from jbi import briefs
        sample = task("a", owns=("src/a.py",))
        t_brief = briefs.render(task=sample, role="review", stage="test-audit", attempt=0, receipt=Path("/tmp/r"))
        m_brief = briefs.render(task=sample, role="review", stage="methodology-audit", attempt=0, receipt=Path("/tmp/r"))
        d_brief = briefs.render(task=sample, role="review", stage="dry-run", attempt=0, receipt=Path("/tmp/r"))
        self.assertIn("confirm they actually prove the behaviour", t_brief)
        self.assertIn("tg work:ready", m_brief)
        self.assertIn("no attribution lines, no --amend, atomic commits", m_brief)
        self.assertIn("in-weight rehearsal of the app itself", d_brief)

    def test_escalation_moves_the_implementer_up(self):
        self.assertEqual(rigor.tier_for("economy", "implement", 0), "S")
        self.assertEqual(rigor.tier_for("economy", "implement", 2), "L")
        self.assertEqual(rigor.tier_for("micro", "verify", 9), "XS")

    def test_task_override_wins(self):
        config = {"rigor": "high", "profile": "balanced"}
        self.assertEqual(rigor.effective({"override": {"rigor": "vibe"}}, config), ("vibe", "balanced"))

    def test_review_stages_fanout(self):
        stages = rigor.review_stages()
        self.assertEqual(len(stages), rigor.REVIEW_FANOUT)
        self.assertEqual(stages[0].name, rigor.REVIEW_CASCADE_STAGE)
        self.assertEqual(tuple(s.name for s in stages[1:]), rigor.REVIEW_DEEPSEEK_STAGES)
        self.assertTrue(all(s.role == "review" for s in stages))


class CeremonyPoolAndLedgerTest(unittest.TestCase):
    def test_window_pool_persists_across_restart(self):
        from jbi.ceremony import WindowPool
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "window_pool.json"
            pool = WindowPool(reg, windows=("work1", "work2"))
            w1 = pool.take_free("D01")
            self.assertEqual(w1, "work1")
            pool.mark_busy("work1", "D01", title="Data contracts", pr_number=812,
                           head_sha="abc1234", ci_status="submitted", attempt=1)
            reloaded = WindowPool(reg, windows=("work1", "work2", "work3"))
            snap = reloaded.snapshot()
            self.assertEqual(snap["busy_count"], 1)
            self.assertEqual(snap["free_count"], 2)
            self.assertEqual(snap["windows"]["work1"]["status"], "busy")
            self.assertEqual(snap["windows"]["work1"]["task"], "D01")
            self.assertEqual(snap["windows"]["work1"]["pr_number"], 812)
            self.assertEqual(reloaded.take_free("D01"), "work1")
            self.assertEqual(reloaded.take_free("D02"), "work2")
            reloaded.mark_free("work1")
            again = WindowPool(reg)
            self.assertEqual(again.snapshot()["busy_count"], 0)
            self.assertEqual(again.snapshot()["free_count"], 3)

    def test_poll_sb_receipt_reads_sqlite_switchboard(self):
        import sqlite3
        from jbi.ceremony import poll_sb_receipt
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "board.db"
            with sqlite3.connect(str(db)) as conn:
                conn.execute("CREATE TABLE sb_messages (id INTEGER PRIMARY KEY, from_slot TEXT, to_slot TEXT, text TEXT)")
                conn.execute("INSERT INTO sb_messages VALUES (10, 'work1', 'main', 'BRIEF D01 | deadbeef | PASS\ntokens: 1450\nall green')")
            rec = poll_sb_receipt("work1", "D01", 0, Path(tmp) / "missing.txt", board_db=db, timeout=1.0)
            self.assertTrue(rec.passed)
            self.assertEqual(rec.task, "D01")
            self.assertEqual(rec.sha, "deadbeef")
            self.assertEqual(rec.tokens, 1450)
            self.assertEqual(rec.findings, ["all green"])


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

    def test_ci_notice_matches_pr_and_head_and_records_structured_findings(self):
        plan = waves.compile_plan([task("core")], 1)
        plan["tasks"]["core"]["pr_number"] = 812
        plan["tasks"]["core"]["commit"] = "ec95b9dbd0123456789abcdef0123456789abcde"
        self.store.commit("plan.updated", Changes().replace("plan", plan).replace("run", "phase", "execute"))
        self.send("ci.notice", {
            "schema": "illm.ci.failure.v1",
            "job_id": "20261116-183000-l0-gA",
            "pr_number": 812,
            "head_sha": "ec95b9dbd0123456789abcdef0123456789abcde",
            "verdict": "red",
            "failed_gates": ["backend-unit", "authority"],
            "evidence_url": "file:///tmp/result.json",
            "first_failure_excerpt": "412: FAIL src/auth/authority.spec.ts",
            "retry_allowed": True,
        })
        findings = "\n".join(self.store.read("/plan/tasks/core/findings"))
        self.assertIn("20261116-183000-l0-gA", findings)
        self.assertIn("backend-unit, authority", findings)
        self.assertIn("file:///tmp/result.json", findings)
        self.assertIn("412: FAIL src/auth/authority.spec.ts", findings)
        with self.assertRaises(ProtocolError) as caught:
            self.send("ci.notice", {"pr_number": 812, "head_sha": "wrong-sha", "verdict": "red"})
        self.assertEqual(caught.exception.code, "conflict")


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

    def test_a_base_path_mounts_the_whole_dashboard_under_a_prefix(self):
        """A reverse proxy forwards a prefix; the page and its assets still resolve.

        The proxy is the only caller that knows the prefix, so the server strips it
        before routing. Every relative link in the page then works unchanged.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = new_store(Path(tmp.name))
        self.addCleanup(store.close)
        auth = Auth()
        app = App(store, Engine(store, None, None), auth,
                  link_bases=lambda: [], base_path="/jbi/preview",
                  public_paths=frozenset({"/api/v1/preview"}))
        server = Server(app, ["127.0.0.1"]).start()
        self.addCleanup(server.stop)
        base = f"http://127.0.0.1:{server.port}"

        # The bare path is not routed: the prefix is part of the address.
        with self.assertRaises(urllib.error.HTTPError) as bare:
            urllib.request.urlopen(base + "/", timeout=5)
        self.assertEqual(bare.exception.code, 404)

        # The page answers under the prefix, unauthenticated, as a proxy forwards it.
        with urllib.request.urlopen(base + "/jbi/preview/", timeout=5) as page:
            self.assertEqual(page.status, 200)
            self.assertIn(b"just-build-it", page.read())

        # A published read-only route needs no session; state still does.
        with urllib.request.urlopen(base + "/jbi/preview/api/v1/preview", timeout=5) as preview:
            self.assertEqual(preview.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as state:
            urllib.request.urlopen(base + "/jbi/preview/api/v1/state", timeout=5)
        self.assertEqual(state.exception.code, 401)

    def test_the_registry_record_names_the_dag_and_carries_no_token(self):
        """A proxy finds the preview through this file, so it must hold no secret."""
        from jbi import cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "jbi-preview.json"

        class Args:
            registry = path
            preview = True
            dag = Path("/tmp/example-dag.json")

        run = cli.Run.__new__(cli.Run)
        run.server = type("S", (), {"port": 9999})()
        run.app = type("A", (), {"base_path": "/jbi/preview", "public_paths": ()})()
        run.run_id = "r-test"
        run._publish_registry(Args())
        record = json.loads(path.read_text())
        self.assertEqual(record["dag"], "/tmp/example-dag.json")
        self.assertEqual(record["base_path"], "/jbi/preview")
        self.assertEqual(record["url"], "http://127.0.0.1:9999/jbi/preview")
        self.assertNotIn("token", record)
        self.assertEqual(sorted(record["published"]), [])

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

    def test_metrics_endpoint_reads_all_six_sources_with_no_llm(self):
        import sqlite3
        from jbi import ceremony, metrics, worktrees as wt
        root = Path(self.tmp.name) / "metrics_fx"
        glm_dir = root / "glm-proxy"
        claude_dir = root / "claude" / "proj1"
        workspace = root / "workspace"
        train_hist = root / "ci-train" / "history.jsonl"
        board_db = root / "board.db"
        ledger_path = root / "task_ledger.jsonl"
        pool_path = root / "window_pool.json"

        glm_dir.mkdir(parents=True)
        (glm_dir / "gemini-usage.jsonl").write_text(
            json.dumps({"ts": "2026-11-15T10:00:00Z", "input_tokens": 1000, "output_tokens": 200}) + "\n"
            + json.dumps({"ts": "2026-11-16T11:00:00Z", "input_tokens": 500, "output_tokens": 100}) + "\n"
        )
        claude_dir.mkdir(parents=True)
        (claude_dir / "sess1.jsonl").write_text(
            json.dumps({
                "type": "assistant",
                "timestamp": "2026-11-16T12:00:00Z",
                "message": {"usage": {"input_tokens": 300, "cache_read_input_tokens": 700, "output_tokens": 50}},
            }) + "\n"
        )

        ledger = ceremony.TaskLedger(ledger_path)
        ledger.record_done(
            task="D01", title="Core", window="work1", pr_number=801, head_sha="aaa111",
            attempts=1, tokens=1200, started_at="2026-11-15T10:00:00Z",
            completed_at="2026-11-15T10:02:00Z", duration_s=120.0,
        )
        ledger.record_done(
            task="D02", title="API", window="work2", pr_number=802, head_sha="bbb222",
            attempts=2, tokens=2400, started_at="2026-11-16T10:00:00Z",
            completed_at="2026-11-16T10:04:00Z", duration_s=240.0,
        )

        wt.init_repo(workspace)
        (workspace / "a.txt").write_text("1\n")
        wt.commit_all(workspace, "[work1] First feature (#801)")
        (workspace / "b.txt").write_text("2\n")
        sha2 = wt.commit_all(workspace, "[work2] Second feature (#802)")
        wt.git(workspace, "update-ref", "refs/remotes/origin/develop", sha2)

        train_hist.parent.mkdir(parents=True)
        train_hist.write_text(
            json.dumps({
                "train_id": "20261116-135337-l1-gB",
                "lane": 1,
                "prs": [812],
                "head_sha": "deadbeef1234",
                "verdict": "running",
            }) + "\n"
            + json.dumps({
                "record": "merge",
                "train_id": "20261116-135407-l3-gB",
                "input_key": "k1",
                "stamped": "2026-11-16T14:40:00Z",
                "merge": {
                    "attempted": False,
                    "state": "green-waiting-other-lanes",
                    "stopped": "merge-serialized",
                    "other_lanes": ["lane 1 gate 6040"],
                },
            }) + "\n"
        )

        with sqlite3.connect(str(board_db)) as conn:
            conn.execute("CREATE TABLE todo_items (id INTEGER PRIMARY KEY, state TEXT)")
            conn.executemany(
                "INSERT INTO todo_items (state) VALUES (?)",
                [("todo",), ("doing",), ("halted",), ("done",), ("done",), ("dropped",)],
            )

        pool = ceremony.WindowPool(pool_path, windows=("work1", "work2"))
        pool.mark_busy("work1", "D03", title="UI metrics", pr_number=815, head_sha="c0ffee12", ci_status="submitted")

        self.server.app.metrics_sources = metrics.MetricsSources(
            glm_proxy_dir=glm_dir,
            claude_projects_dir=root / "claude",
            task_ledger_path=ledger_path,
            workspace_repo=workspace,
            ci_train_history=train_hist,
            board_db=board_db,
            window_registry_path=pool_path,
            days=14,
        )
        status, body = self.call("GET", "/api/v1/metrics")
        self.assertEqual(status, 200)
        m = body["metrics"]
        self.assertTrue(m["tokens"]["available"])
        self.assertEqual(m["tokens"]["total"], 1200 + 600 + 1050)
        self.assertEqual(m["averages"]["tokens_per_task"], 1800.0)
        self.assertEqual(m["averages"]["time_per_task_s"], 180.0)
        self.assertTrue(m["prs_merged"]["available"])
        self.assertEqual(m["prs_merged"]["total"], 2)
        self.assertTrue(m["ci_train"]["available"])
        self.assertEqual(m["ci_train"]["train_state"], "green-waiting-other-lanes")
        self.assertEqual(len(m["ci_train"]["lanes"]), 1)
        self.assertEqual(len(m["ci_train"]["held_greens"]), 1)
        self.assertTrue(m["board"]["available"])
        self.assertEqual((m["board"]["open_rows"], m["board"]["done_rows"]), (3, 2))
        self.assertTrue(m["window_pool"]["available"])
        self.assertEqual((m["window_pool"]["busy_count"], m["window_pool"]["free_count"]), (1, 1))
        self.assertEqual(m["right_now"]["busy_windows"][0]["task"], "D03")
        self.assertEqual({p["pr_number"] for p in m["right_now"]["prs_in_ci"]}, {815, 812})

    def test_metrics_missing_sources_report_unavailable_with_path_and_no_invented_numbers(self):
        from jbi import metrics
        missing = Path(self.tmp.name) / "does-not-exist"
        self.server.app.metrics_sources = metrics.MetricsSources(
            glm_proxy_dir=missing / "glm",
            claude_projects_dir=missing / "claude",
            task_ledger_path=missing / "task_ledger.jsonl",
            workspace_repo=missing / "workspace",
            ci_train_history=missing / "history.jsonl",
            board_db=missing / "board.db",
            window_registry_path=missing / "window_pool.json",
        )
        status, body = self.call("GET", "/api/v1/metrics")
        self.assertEqual(status, 200)
        m = body["metrics"]
        for section, expected_subpath in (
            ("tokens", "glm"),
            ("task_ledger", "task_ledger.jsonl"),
            ("prs_merged", "workspace"),
            ("ci_train", "history.jsonl"),
            ("board", "board.db"),
            ("window_pool", "window_pool.json"),
        ):
            with self.subTest(section=section):
                self.assertFalse(m[section]["available"])
                self.assertEqual(m[section]["status"], "unavailable")
                self.assertIn(expected_subpath, m[section]["path"])
        self.assertIsNone(m["tokens"]["total"])
        self.assertIsNone(m["averages"]["tokens_per_task"])
        self.assertIsNone(m["averages"]["time_per_task_s"])
        self.assertIsNone(m["prs_merged"]["total"])
        self.assertIsNone(m["board"]["open_rows"])
        self.assertIsNone(m["board"]["done_rows"])
        self.assertIsNone(m["window_pool"]["busy_count"])
        self.assertIsNone(m["window_pool"]["free_count"])

    def test_preview_endpoint_returns_waves_critical_path_and_etas(self):
        plan = waves.compile_plan([task("core"), task("api", "core"), task("ui", "core"), task("e2e", "api", "ui")], 1)
        self.store.commit("plan.updated", Changes()
                          .replace("plan", plan)
                          .set("config", "dry_run", True)
                          .set("config", "preview", True))
        status, body = self.call("GET", "/api/v1/preview")
        self.assertEqual(status, 200)
        p = body["preview"]
        self.assertTrue(p["dry_run"])
        self.assertEqual(p["total_tasks"], 4)
        self.assertEqual(p["waves_total"], 3)
        self.assertEqual([w["task_ids"] for w in p["waves"]], [["core"], ["api", "ui"], ["e2e"]])
        self.assertTrue(p["waves"][1]["parallel"])
        self.assertEqual(len(p["critical_path"]), 3)
        self.assertGreater(p["critical_path_eta_s"], 0)
        self.assertEqual(p["remaining_eta_s"], p["critical_path_eta_s"])


class InstallAndResumeTest(unittest.TestCase):
    def test_prepare_resume_restores_in_flight_and_unblocked_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            plan = waves.compile_plan([task("a"), task("b", "a"), task("c")], 1)
            plan["tasks"]["a"]["status"] = "merged"
            plan["tasks"]["a"]["stages"] = {"implement": {"status": "passed"}, "merge": {"status": "passed"}}
            plan["tasks"]["b"]["status"] = "blocked"
            plan["tasks"]["c"]["status"] = "verifying"
            plan["tasks"]["c"]["stages"] = {
                "implement": {"status": "passed"},
                "acceptance": {"status": "passed"},
                "verify": {"status": "running"},
            }
            store.commit("plan.updated", Changes()
                         .replace("plan", plan)
                         .replace("run", "phase", "execute")
                         .replace("run", "stopping", {"mode": "standdown", "since": "now"})
                         .replace("run", "busy", {"label": "Thinking", "since": "now"}))
            resumed = cli.prepare_resume(store)
            self.assertEqual(sorted(resumed), ["b", "c"])
            self.assertIsNone(store.read("/run/stopping"))
            self.assertIsNone(store.read("/run/busy"))
            self.assertEqual(store.read("/plan/tasks/a/status"), "merged")
            self.assertEqual(store.read("/plan/tasks/b/status"), "ready")
            self.assertEqual(store.read("/plan/tasks/c/status"), "ready")
            self.assertEqual(store.read("/plan/tasks/c/resume_after"), "acceptance")
            store.close()
            args = cli.parse_args(["resume", "--if-exists", "--no-menu", "--no-tailscale"])
            self.assertEqual((args.command, args.if_exists, args.no_menu, args.no_tailscale),
                             ("resume", True, True, True))

    def test_install_and_uninstall_scripts_render_unit_and_enable_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            ctl_log = Path(tmp) / "systemctl.log"
            fake_ctl = Path(tmp) / "fake-systemctl"
            fake_ctl.write_text(f"#!/usr/bin/env bash\necho \"$*\" >> {str(ctl_log)!r}\n")
            fake_ctl.chmod(0o755)
            env = {**os.environ, "HOME": str(fake_home), "SYSTEMCTL": str(fake_ctl)}

            inst = subprocess.run(["bash", str(ROOT / "install.sh")], cwd=str(ROOT), env=env,
                                  capture_output=True, text=True, check=False)
            self.assertEqual(inst.returncode, 0, inst.stderr or inst.stdout)
            unit_file = fake_home / ".config" / "systemd" / "user" / "just-build-it.service"
            self.assertTrue(unit_file.is_file())
            unit_text = unit_file.read_text()
            self.assertIn(f"WorkingDirectory={ROOT}", unit_text)
            self.assertIn("ExecStart=/usr/bin/python3 -m jbi resume --if-exists --no-menu --no-tailscale", unit_text)
            self.assertTrue((fake_home / ".local" / "bin" / "just-build-it").is_symlink())
            self.assertIn("--user daemon-reload", ctl_log.read_text())
            self.assertIn("--user enable just-build-it.service", ctl_log.read_text())

            uninst = subprocess.run(["bash", str(ROOT / "uninstall.sh"), "--purge"], cwd=str(ROOT), env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(uninst.returncode, 0, uninst.stderr or uninst.stdout)
            self.assertFalse(unit_file.exists())
            self.assertFalse((fake_home / ".local" / "bin" / "just-build-it").exists())
            self.assertFalse((fake_home / ".just-build-it").exists())


class DagSubtasksAndToolTest(unittest.TestCase):
    def test_expand_d03_six_batches_into_six_tasks_in_right_waves(self):
        dag_doc = {
            "project": "tangram-v1",
            "repo": ".",
            "mode": "existing",
            "nodes": [
                {"id": "D01", "title": "Native table", "deps": [], "acceptance": "true"},
                {"id": "P01", "title": "Project scope", "deps": [], "acceptance": "true"},
                {"id": "D02", "title": "Native storage", "deps": ["D01", "P01"], "acceptance": "true"},
                {"id": "E01", "title": "Outbox", "deps": ["D01", "P01"], "acceptance": "true"},
                {
                    "id": "D03",
                    "title": "Shared Artifact core and Library identity",
                    "deps": ["D01", "P01", "E01", "D02"],
                    "owner": "work13",
                    "acceptance": "node scripts/verify-launch-artifact-core.mjs --require-cases 16",
                    "subtasks": [
                        {"id": "D03-B1", "batch": "B1", "title": "D01 imports and pure record validation", "deps": [], "owns": ["src/a1.ts"]},
                        {"id": "D03-B2", "batch": "B2", "title": "Atomic source publication and Library pointer", "deps": ["D03-B1"], "owns": ["src/a2.ts"]},
                        {"id": "D03-B3", "batch": "B3", "title": "Render job fencing and proxy outputs", "deps": ["D03-B2"], "owns": ["src/a3.ts"]},
                        {"id": "D03-B4", "batch": "B4", "title": "Lease takeover and anchors", "deps": ["D03-B3"], "owns": ["src/a4.ts"]},
                        {"id": "D03-B5", "batch": "B5", "title": "Privacy integration and orphan sweep", "deps": ["D03-B4"], "owns": ["src/a5.ts"]},
                        {"id": "D03-B6", "batch": "B6", "title": "Two review rounds and verification evidence", "deps": ["D03-B5"], "owns": ["src/a6.ts"]},
                    ],
                },
                {"id": "L01", "title": "Library upload", "deps": ["D03"], "acceptance": "true"},
            ],
        }
        plan = dag_loader.load(json.dumps(dag_doc))
        self.assertEqual(
            plan["waves"],
            [
                ["D01", "P01"],
                ["D02", "E01"],
                ["D03-B1"],
                ["D03-B2"],
                ["D03-B3"],
                ["D03-B4"],
                ["D03-B5"],
                ["D03-B6"],
                ["L01"],
            ],
        )
        self.assertEqual(plan["tasks"]["D03-B1"]["deps"], ["D01", "P01", "E01", "D02"])
        self.assertEqual(plan["tasks"]["D03-B2"]["deps"], ["D01", "P01", "E01", "D02", "D03-B1"])
        self.assertEqual(plan["tasks"]["D03-B6"]["node"], "D03")
        self.assertEqual(plan["tasks"]["D03-B6"]["batch"], "B6")
        self.assertIn("D03-B6", plan["tasks"]["L01"]["deps"])

    def test_dag_tool_subcommands_and_refusal_of_invalid_edits(self):
        from jbi import dag_tool
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "work.json"
            self.assertEqual(
                dag_tool.main(["new", str(json_path), "--project", "demo", "--id", "A01", "--title", "Auth base"]),
                0,
            )
            self.assertEqual(
                dag_tool.main([
                    "add-task", str(json_path),
                    "--id", "D03", "--title", "Artifact core",
                    "--deps", "A01", "--owns", "src/artifacts/core.ts",
                ]),
                0,
            )
            self.assertEqual(
                dag_tool.main(["add-Task" if False else "add-task", str(json_path), "--id", "I01", "--title", "Connectors"]),
                0,
            )
            self.assertEqual(
                dag_tool.main(["add-dep", str(json_path), "--task", "I01", "--dep", "A01"]),
                0,
            )

            # Refuse a cycle without mutating the file on disk.
            before_cycle = json_path.read_text()
            self.assertEqual(
                dag_tool.main(["add-dep", str(json_path), "--task", "A01", "--dep", "D03"]),
                1,
            )
            self.assertEqual(json_path.read_text(), before_cycle)

            # Refuse an invalid owned path without mutating the file on disk.
            self.assertEqual(
                dag_tool.main([
                    "add-task", str(json_path),
                    "--id", "BAD", "--title", "Bad owns", "--owns", "../escape.ts",
                ]),
                1,
            )
            self.assertEqual(json_path.read_text(), before_cycle)

            # Set explicit wave and validate.
            self.assertEqual(
                dag_tool.main(["set-wave", str(json_path), "--wave", "2", "--tasks", "D03,I01"]),
                0,
            )
            self.assertEqual(dag_tool.main(["validate", str(json_path)]), 0)
            self.assertEqual(dag_tool.main(["show", str(json_path)]), 0)

            # Remove I01 and verify D03 remains valid.
            self.assertEqual(dag_tool.main(["remove-task", str(json_path), "--id", "I01"]), 0)

            # Add subtasks to D03 and list them.
            self.assertEqual(
                dag_tool.main([
                    "add-subtask", str(json_path),
                    "--node", "D03", "--id", "D03-B1", "--batch", "B1",
                    "--title", "Batch 1", "--owns", "src/b1.ts",
                ]),
                0,
            )
            self.assertEqual(
                dag_tool.main([
                    "add-subtask", str(json_path),
                    "--node", "D03", "--id", "D03-B2", "--batch", "B2",
                    "--title", "Batch 2", "--deps", "D03-B1", "--owns", "src/b2.ts",
                ]),
                0,
            )
            self.assertEqual(dag_tool.main(["list-subtasks", str(json_path), "--node", "D03", "--json"]), 0)
            expanded_plan = dag_loader.load(json_path)
            self.assertEqual(expanded_plan["waves"], [["A01"], ["D03-B1"], ["D03-B2"]])


if __name__ == "__main__":
    unittest.main()
