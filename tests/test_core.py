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

    def test_owns_conflict_prefixes_and_wildcards(self):
        plan = waves.compile_plan([
            task("empty1"),
            task("empty2"),
            task("dir_src", owns=("src",)),
            task("file_src", owns=("src/a.py",)),
            task("sibling_src", owns=("src/ab.py",)),
            task("sub_dir", owns=("src/a",)),
            task("glob_tree", owns=("src/**",)),
            task("glob_py", owns=("src/*.py",)),
            task("file_ts", owns=("src/a.ts",)),
            task("nested_py", owns=("src/deep/mod.py",)),
            task("wild_all", owns=("**",)),
            task("wild_star", owns=("*",)),
        ], 1)
        self.assertIsNone(waves.owns_conflict(plan, "empty1", ["empty2"]))
        self.assertIsNone(waves.owns_conflict(plan, "empty1", ["file_src"]))
        self.assertIsNone(waves.owns_conflict(plan, "file_src", ["empty1"]))
        self.assertEqual(waves.owns_conflict(plan, "wild_all", ["empty1"]), "empty1")
        self.assertEqual(waves.owns_conflict(plan, "empty1", ["wild_all"]), "wild_all")
        self.assertEqual(waves.owns_conflict(plan, "wild_all", ["file_src"]), "file_src")
        self.assertEqual(waves.owns_conflict(plan, "wild_star", ["file_src"]), "file_src")
        self.assertEqual(waves.owns_conflict(plan, "dir_src", ["file_src"]), "file_src")
        self.assertEqual(waves.owns_conflict(plan, "file_src", ["dir_src"]), "dir_src")
        self.assertIsNone(waves.owns_conflict(plan, "sub_dir", ["sibling_src"]))
        self.assertEqual(waves.owns_conflict(plan, "glob_tree", ["nested_py"]), "nested_py")
        self.assertEqual(waves.owns_conflict(plan, "glob_py", ["file_src"]), "file_src")
        self.assertIsNone(waves.owns_conflict(plan, "glob_py", ["file_ts"]))

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
        self.assertIn("CI evidence", m_brief)
        self.assertIn("governed repository test", m_brief)
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

    def test_fast_rigor_pipeline_and_protocol_validation(self):
        stages = rigor.pipeline("fast")
        self.assertEqual(
            [(s.name, s.role, s.label) for s in stages],
            [
                ("implement", "implement", "Implement"),
                ("acceptance", "acceptance", "Acceptance"),
                ("review-1", "review", "Review (diff)"),
                ("merge", "merge", "Train merge"),
            ],
        )
        self.assertEqual([s.name for s in stages if s.role in rigor.AGENT_ROLES], ["implement", "review-1"])
        self.assertEqual(rigor.LEVELS["fast"]["lane_checks"], ("build", "test"))
        _, _, cfg = protocol.validate_command(
            {"id": "c-fast", "type": "run.configure", "payload": {"rigor": "fast", "profile": "balanced"}}
        )
        self.assertEqual(cfg["rigor"], "fast")
        _, _, ovr = protocol.validate_command(
            {"id": "c-ovr", "type": "task.override", "payload": {"task": "core", "rigor": "fast"}}
        )
        self.assertEqual(ovr["rigor"], "fast")


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

    def test_parse_agent_receipt_reads_the_local_receipt(self):
        from jbi.ceremony import parse_agent_receipt
        rec = parse_agent_receipt("BRIEF D01 | deadbeef | PASS\ntokens: 1450\nall green", "D01")
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

    def test_notice_without_a_runtime_cannot_mark_a_merge(self):
        plan = waves.compile_plan([task("core")], 1)
        plan["tasks"]["core"]["pr_number"] = 812
        plan["tasks"]["core"]["commit"] = "ec95b9dbd0123456789abcdef0123456789abcde"
        self.store.commit("plan.updated", Changes().replace("plan", plan).replace("run", "phase", "execute"))
        with self.assertRaises(ProtocolError) as caught:
            self.send("ci.notice", {"pr_number": 812,
                                     "head_sha": "ec95b9dbd0123456789abcdef0123456789abcde",
                                     "verdict": "green", "merge_sha": "ffff"})
        self.assertEqual(caught.exception.code, "conflict")
        self.assertNotEqual(self.store.read("/plan/tasks/core/status"), "merged")
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


class NodeRoutingTest(unittest.TestCase):
    """A DAG node may pin its provider and model (founder order 2026-11-17)."""

    def test_a_node_override_reaches_model_selection(self):
        from jbi import dag_loader, models
        doc = {"tasks": [{"id": "X1", "title": "t", "goal": "g", "override": {
            "prefer": [{"provider": "agy", "model": "gemini-4-argon-high"}]}}]}
        path = Path(tempfile.mkdtemp()) / "d.json"
        path.write_text(json.dumps(doc))
        task, = dag_loader.load_tasks(path)
        state = {"config": {"profile": "balanced"},
                 "accounts": {"agy": {"enabled": True, "status": "ok"}}}
        choice = models.select(state, "implement", task=task)
        self.assertEqual((choice.provider, choice.model), ("agy", "gemini-4-argon-high"))

    def test_a_preference_falls_through_when_its_provider_is_unavailable(self):
        """The rule the founder stated: prefer the top providers, else the cascade."""
        from jbi import dag_loader, models
        doc = {"tasks": [{"id": "X1", "title": "t", "goal": "g", "override": {
            "prefer": [{"provider": "agy"}, {"provider": "codex"}]}}]}
        path = Path(tempfile.mkdtemp()) / "d.json"
        path.write_text(json.dumps(doc))
        task, = dag_loader.load_tasks(path)
        # No provider enabled: the preference cannot be served, so it falls through.
        state = {"config": {"profile": "balanced"}, "accounts": {}}
        choice = models.select(state, "implement", task=task)
        self.assertNotIn(choice.provider, ("agy", "codex"),
                         "a preference was served although neither provider is enabled")

    def test_a_model_without_a_provider_is_refused(self):
        """A model id alone does not name the CLI that serves it, so refuse it.

        A silent fall back to the cascade is the dangerous case: a node meant for
        agy would run elsewhere and nothing would say so.
        """
        from jbi import dag_loader
        doc = {"tasks": [{"id": "X1", "title": "t", "goal": "g", "override": {"model": "gemini-4-argon-high"}}]}
        path = Path(tempfile.mkdtemp()) / "d.json"
        path.write_text(json.dumps(doc))
        with self.assertRaises(dag_loader.Invalid) as caught:
            dag_loader.load_tasks(path)
        self.assertIn("unknown key", str(caught.exception))

    def test_an_unknown_provider_is_refused(self):
        from jbi import dag_loader
        doc = {"tasks": [{"id": "X1", "title": "t", "goal": "g", "override": {"pin": {"provider": "openai", "model": "m"}}}]}
        path = Path(tempfile.mkdtemp()) / "d.json"
        path.write_text(json.dumps(doc))
        with self.assertRaises(dag_loader.Invalid):
            dag_loader.load_tasks(path)


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

    def test_metrics_endpoint_reads_internal_fleet_with_no_llm(self):
        from jbi import tracking, worktrees as wt
        from unittest import mock
        root = Path(self.tmp.name) / "metrics_repo"
        wt.init_repo(root)
        (root / "README").write_text("base\n")
        base = wt.commit_all(root, "base")
        tr = tracking.Tracking(self.store.path.parent / "fleet.db")
        for number, (tid, day, seconds) in enumerate((("D01", "2026-11-15", 120), ("D02", "2026-11-16", 240)), 800):
            path = wt.create_worktree(root, tid, base, root.parent / "trees")
            with mock.patch.object(tracking, "_now", return_value=f"{day}T10:00:00Z"):
                tr.create_task(title=tid, owns=[f"src/{tid}.txt"], branch=f"jbi/{tid}",
                               worktree=path, base_commit=base, task_id=tid)
                grant = tr.dispatch(tid, session_id=f"s-{tid}", owns=[f"src/{tid}.txt"], branch=f"jbi/{tid}",
                                    worktree=path, continuation=False)
            (path / "src").mkdir()
            (path / f"src/{tid}.txt").write_text(tid + "\n", encoding="utf-8")
            head = wt.commit_all(path, tid + ": write owned file")
            tr.record_commit(tid, head)
            split = 2400 if number == 801 else 3600
            tr.record_cost(grant["dispatch_id"], input_tokens=split // 2, output_tokens=split // 2,
                           cache_read=100 * (number - 799), cache_write=5 * (number - 799))
            tr.record_ci(tid, pr_number=number, head_sha=head, base_commit=base,
                         green_run=f"job-{number}", squash_sha=base,
                         service_reply={"type": "data", "text": "verdict GREEN"}, state="green")
            with mock.patch.object(tracking, "_now", side_effect=[f"{day}T10:00:00Z", f"{day}T10:0{seconds // 60}:00Z"]):
                tr.mark_task_state(tid, "merging")
                tr.record_merge(tid, number, head, base, f"job-{number}")
            tr.release(tid, reason="merged", repository=root)
        tr.close()
        status, body = self.call("GET", "/api/v1/metrics")
        self.assertEqual(status, 200)
        m = body["metrics"]
        self.assertTrue(m["tokens"]["available"])
        self.assertEqual(m["tokens"]["total"], 6000)
        self.assertEqual(m["averages"]["tokens_per_task"], 3000.0)
        self.assertEqual(m["averages"]["time_per_task_s"], 180.0)
        self.assertEqual(m["prs_merged"]["total"], 2)
        self.assertEqual(len(m["merges"]), 2)
        self.assertEqual(len(m["ci_submissions"]), 2)
        self.assertTrue(all(row["released_at"] for row in m["leases"]))

    def test_metrics_missing_store_reports_unavailable_with_path_and_no_invented_numbers(self):
        status, body = self.call("GET", "/api/v1/metrics")
        self.assertEqual(status, 200)
        m = body["metrics"]
        for section in ("tokens", "task_ledger", "prs_merged"):
            self.assertFalse(m[section]["available"])
            self.assertEqual(m[section]["status"], "unavailable")
            self.assertIn("fleet.db", m[section]["path"])
        self.assertIsNone(m["tokens"]["total"])
        self.assertIsNone(m["averages"]["tokens_per_task"])
        self.assertIsNone(m["averages"]["time_per_task_s"])
        self.assertIsNone(m["prs_merged"]["total"])
        self.assertEqual(m["leases"], [])

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
            tools = Path(tmp) / "bin"
            tools.mkdir()
            for name in ("ci-conductor",):
                path = tools / name
                path.write_text("#!/usr/bin/env bash\nexit 0\n")
                path.chmod(0o755)
            suffix = os.environ.get("PATH", "")
            env = {**os.environ, "HOME": str(fake_home), "SYSTEMCTL": str(fake_ctl),
                   "PATH": os.pathsep.join([str(tools), suffix])}

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

    def test_brief_renders_rich_spec_fields_for_smaller_models(self):
        from jbi import briefs
        rich_task = waves.normalize({
            "id": "D03-B1",
            "title": "D03 B1: Core ports",
            "goal": "Implement D03 Batch B1 core ports and entities.",
            "deps": [],
            "owns": ["src/a.ts"],
            "acceptance": "npx tsc --noEmit",
            "path": "04-tables-docs-slides-charts/D03-PLAN.md",
            "spec": "04-tables-docs-slides-charts/D03-SPEC.md",
            "spec_details": ["Extend D02 artifact_heads without creating local grant stores."],
            "contracts": ["ArtifactSourcePolicyPort", "ArtifactOutputPolicyPort"],
            "invariants": ["All content queries include trusted tenantId and projectId."],
            "test_cases": ["D03-T01: preserve D01 identities"],
            "done_criteria": ["tsc --noEmit exits 0"],
        })
        rendered = briefs.render(task=rich_task, role="implement", stage="implement", attempt=0, receipt=Path("/tmp/r"))
        for heading in (
            "## Source Docs",
            "## Specification Details",
            "## Contracts & Interfaces",
            "## Invariants",
            "## Required Test Cases",
            "## Done Criteria",
        ):
            self.assertIn(heading, rendered)

    def test_brief_truncates_middle_and_keeps_header_and_footer_intact(self):
        from jbi import briefs
        huge_task = waves.normalize({
            "id": "D03-B9",
            "title": "D03 B9: Large specification task",
            "goal": "Implement D03 Batch B9 with full safety rules.",
            "deps": [],
            "owns": ["src/d03/b9.ts", "tests/d03/b9.spec.ts"],
            "acceptance": "npx vitest run tests/d03/b9.spec.ts",
            "path": "04/D03-PLAN.md",
            "spec": "04/D03-SPEC.md",
            "spec_details": [f"Spec detail line {i}" for i in range(40)],
            "contracts": [f"ContractPort{i}" for i in range(25)],
            "invariants": [f"Invariant rule {i}" for i in range(25)],
            "test_cases": [f"D03-T{i:02d}: test case" for i in range(20)],
            "done_criteria": [f"Criterion {i}" for i in range(15)],
        })
        receipt_file = Path("/tmp/run/receipts/D03-B9-implement-1.txt")
        rendered = briefs.render(
            task=huge_task,
            role="implement",
            stage="implement",
            attempt=1,
            receipt=receipt_file,
            findings=[f"Prior failure {i}" for i in range(15)],
        )
        lines = rendered.splitlines()
        self.assertEqual(len(lines), briefs.MAX_LINES)
        self.assertIn("# Brief D03-B9 — implement (attempt 2)", rendered)
        self.assertIn("Task: D03 B9: Large specification task", rendered)
        self.assertIn("## Goal", rendered)
        self.assertIn("Step 8.", rendered)
        self.assertIn("... (truncated)", rendered)
        self.assertIn("## Owns\n- src/d03/b9.ts\n- tests/d03/b9.spec.ts", rendered)
        self.assertIn("## Acceptance\n`npx vitest run tests/d03/b9.spec.ts` must exit 0.", rendered)
        self.assertIn("## Rules (common, declared once)", rendered)
        for _num, rule_text in briefs.COMMON:
            self.assertIn(rule_text, rendered)
        self.assertIn(f"## Receipt\nWrite {receipt_file}.", rendered)
        self.assertEqual(lines[-1], "Then one finding per line.")


if __name__ == "__main__":
    unittest.main()
