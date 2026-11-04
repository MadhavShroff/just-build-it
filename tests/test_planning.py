"""Tests for the planner, stack proposer, bootstrap, architecture, providers, secrets and skills.

No test calls a real model. Completers are scripted fakes; provider CLIs are fake
executables on a temporary PATH that record their argv.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jbi import architecture, bootstrap, planner, providers, secrets, stack, waves
from jbi.llm import Completion, CompletionError, Prompt

ROOT = Path(__file__).resolve().parent.parent
# Skills are product-neutral: they name no company, person or machine. The public check
# refuses personal paths and addresses; JBI_PRIVATE_TERMS adds a comma-separated private list.
NEUTRAL = (r"/home/\w+", r"/Users/\w+", r"[\w.+-]+@[\w-]+\.[\w.]+", r"\b\d{1,3}(\.\d{1,3}){3}\b")
PRIVATE = tuple(t.strip() for t in os.environ.get("JBI_PRIVATE_TERMS", "").split(",") if t.strip())


class Script:
    """A Completer that returns scripted replies in order and records its prompts."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt: Prompt) -> Completion:
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else "{}"
        if isinstance(reply, Exception):
            raise reply
        return Completion(text=reply if isinstance(reply, str) else json.dumps(reply), model="fake", provider="fake")


def state_with(detail="guided", *texts, plan=None):
    return {"intake": {"detail": detail, "messages": [{"role": "user", "text": t, "ts": "t"} for t in texts]},
            "plan": plan or {}, "run": {}}


def good_plan():
    return {
        "rough_prd": "A bookshelf app.",
        "prd": {"name": "Book Shelf", "problem": "Track books", "users": "readers",
                "features": ["add books", "search books"], "constraints": [], "assumptions": ["web app"],
                "stack_preferences": [],
                "components": [
                    {"id": "api", "name": "API", "kind": "service", "layer": "backend", "tech": "Python",
                     "talks_to": [{"to": "db", "label": "reads", "protocol": "sql"}]},
                    {"id": "db", "name": "Database", "kind": "database", "layer": "data", "tech": "SQLite",
                     "talks_to": []}]},
        "tasks": [
            {"id": "core", "title": "Core", "goal": "Build core", "deps": [], "acceptance": "true",
             "owns": ["src/core.py"], "components": ["api", "db"]},
            {"id": "search", "title": "Search", "goal": "Search", "deps": ["core"], "acceptance": "true",
             "owns": ["src/search.py"], "components": ["api"]}],
    }


class ChatTests(unittest.TestCase):
    def test_each_detail_level_changes_the_instruction(self):
        seen = {}
        for level in ("user", "guided", "delegate"):
            script = Script({"reply": "ok", "ready": False})
            planner.chat_reply(script, state_with(level, "a book app"))
            seen[level] = script.prompts[0].system
        self.assertEqual(len(set(seen.values())), 3)
        self.assertIn("Ask very little", seen["user"])
        self.assertIn("one to three", seen["guided"])
        self.assertIn("at most one question", seen["delegate"])

    def test_reply_and_ready_flag(self):
        reply = planner.chat_reply(Script({"reply": "Who reads?", "ready": True}), state_with("guided", "x"))
        self.assertEqual((reply.text, reply.ready), ("Who reads?", True))

    def test_plain_text_reply_is_not_ready(self):
        reply = planner.chat_reply(Script("What is it for?"), state_with("guided", "x"))
        self.assertEqual((reply.text, reply.ready), ("What is it for?", False))


class ImportTests(unittest.TestCase):
    def test_import_gives_rough_prd(self):
        draft = planner.from_import(Script({"rough_prd": "Build X", "summary": "Two items"}), "- a\n- b")
        self.assertEqual((draft.rough_prd, draft.summary), ("Build X", "Two items"))

    def test_import_with_text_reply_keeps_the_text(self):
        draft = planner.from_import(Script("Rough text"), "- a")
        self.assertEqual(draft.rough_prd, "Rough text")
        self.assertTrue(draft.summary)


class FinishTests(unittest.TestCase):
    def assert_valid(self, result):
        waves.compile_plan(result.tasks, 1)
        for key in ("name", "problem", "users", "features", "constraints", "assumptions",
                    "stack_preferences", "components"):
            self.assertIn(key, result.prd)

    def test_valid_plan(self):
        result = planner.finish(Script(good_plan()), state_with("guided", "bookshelf"))
        self.assert_valid(result)
        self.assertEqual(result.prd["name"], "book-shelf")
        self.assertEqual([t["id"] for t in result.tasks], ["core", "search"])
        self.assertEqual(result.rough_prd, "A bookshelf app.")

    def test_malformed_json_is_repaired_once(self):
        script = Script("not json at all", good_plan())
        result = planner.finish(script, state_with("guided", "bookshelf"))
        self.assertEqual(len(script.prompts), 2)
        self.assertEqual(script.prompts[1].purpose, "planner.repair")
        self.assertIn("no JSON", script.prompts[1].user)
        self.assertEqual([t["id"] for t in result.tasks], ["core", "search"])

    def test_malformed_twice_falls_back_to_feature_plan(self):
        script = Script("nope", "still nope")
        result = planner.finish(script, state_with("guided", "a bookshelf"))
        self.assert_valid(result)
        self.assertEqual(len(script.prompts), 2)
        self.assertEqual(result.tasks[0]["id"], "core")
        self.assertTrue(all(t["deps"] == ["core"] for t in result.tasks[1:]))

    def test_cycle_is_repaired(self):
        cyclic = good_plan()
        cyclic["tasks"][0]["deps"] = ["search"]
        script = Script(cyclic, good_plan())
        result = planner.finish(script, state_with("guided", "bookshelf"))
        self.assertIn("cycle", script.prompts[1].user.lower())
        self.assertEqual(result.tasks[0]["deps"], [])

    def test_persistent_cycle_falls_back(self):
        cyclic = good_plan()
        cyclic["tasks"][0]["deps"] = ["search"]
        result = planner.finish(Script(cyclic, cyclic), state_with("guided", "bookshelf"))
        self.assert_valid(result)
        self.assertEqual(result.tasks[0]["id"], "core")

    def test_model_error_falls_back(self):
        result = planner.finish(Script(CompletionError("down")), state_with("guided", "a todo app"))
        self.assert_valid(result)

    def test_fallback_uses_features_and_unique_ids(self):
        prd = planner.normalize_prd({"name": "x", "features": ["Add books", "Add books!", "123"]}, state_with())
        tasks = planner.fallback_plan(prd)
        waves.validate(tasks)
        self.assertEqual(len({t["id"] for t in tasks}), 4)

    def test_dangling_component_links_are_dropped(self):
        plan = good_plan()
        plan["prd"]["components"][0]["talks_to"].append({"to": "ghost", "label": "", "protocol": ""})
        result = planner.finish(Script(plan), state_with("guided", "x"))
        self.assertEqual([l["to"] for l in result.prd["components"][0]["talks_to"]], ["db"])


class StackTests(unittest.TestCase):
    PRD = {"name": "app", "problem": "p", "features": ["f"], "stack_preferences": []}

    def test_cheapest_first_when_model_output_is_unusable(self):
        proposal = stack.propose(Script("garbage"), self.PRD, [])
        self.assertEqual(proposal.summary, "Python 3.12 · SQLite · htmx · Local machine")
        state = proposal.as_state()
        self.assertFalse(state["accepted"])
        self.assertTrue(all(l["cost"] == "free" for l in state["layers"]))
        self.assertEqual(set(state["layers"][0]), {"layer", "choice", "reason", "cost"})

    def test_model_layers_are_used(self):
        reply = {"layers": [{"layer": "language", "choice": "Go", "reason": "fast", "cost": "free"}]}
        self.assertEqual(stack.propose(Script(reply), self.PRD, []).summary, "Go")

    def test_user_named_stack_has_priority(self):
        messages = [{"role": "user", "text": "Please use PostgreSQL and React"}]
        reply = {"layers": [{"layer": "database", "choice": "SQLite", "reason": "cheap", "cost": "free"},
                            {"layer": "frontend", "choice": "htmx", "reason": "cheap", "cost": "free"}]}
        proposal = stack.propose(Script(reply), self.PRD, messages)
        choices = {l["layer"]: l["choice"] for l in proposal.layers}
        self.assertEqual((choices["database"], choices["frontend"]), ("PostgreSQL", "React"))

    def test_revise_keeps_current_and_applies_request(self):
        first = stack.propose(Script("garbage"), self.PRD, [])
        revised = stack.propose(Script("garbage"), self.PRD, [], request="use MongoDB", current=first.as_state())
        choices = {l["layer"]: l["choice"] for l in revised.layers}
        self.assertEqual(choices["database"], "MongoDB")
        self.assertEqual(choices["language"], "Python 3.12")

    def test_model_error_gives_default(self):
        self.assertTrue(stack.propose(Script(CompletionError("x")), self.PRD, []).summary)


def plan_of(statuses):
    tasks = {tid: {"status": s, "components": c} for tid, (s, c) in statuses.items()}
    return {"tasks": tasks, "order": list(tasks)}


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        prd = good_plan()["prd"]
        self.plan = plan_of({"core": ("ready", ["api", "db"]), "search": ("blocked", ["api"])})
        self.graph = architecture.initial(prd, self.plan)

    def test_initial(self):
        self.assertEqual(self.graph["revision"], 1)
        self.assertEqual(set(self.graph["nodes"]), {"api", "db"})
        self.assertEqual(self.graph["nodes"]["api"]["tasks"], ["core", "search"])
        self.assertEqual(self.graph["nodes"]["api"]["status"], "planned")
        edge = next(iter(self.graph["edges"].values()))
        self.assertEqual((edge["from"], edge["to"], edge["protocol"], edge["status"]), ("api", "db", "sql", "planned"))

    def test_status_sync(self):
        plan = plan_of({"core": ("merged", ["api", "db"]), "search": ("running", ["api"])})
        result = architecture.sync(self.graph, plan, event="task.merged", task="core", patch=None)
        self.assertTrue(result.changed)
        self.assertEqual(result.graph["revision"], 2)
        self.assertEqual(result.graph["nodes"]["api"]["status"], "building")
        self.assertEqual(result.graph["nodes"]["db"]["status"], "built")
        self.assertEqual(next(iter(result.graph["edges"].values()))["status"], "building")
        self.assertEqual(self.graph["nodes"]["db"]["status"], "planned", "input graph is not mutated")

    def test_edge_built_and_failed(self):
        built = architecture.sync(self.graph, plan_of({"core": ("merged", ["api", "db"]),
                                                       "search": ("merged", ["api"])}),
                                  event="wave.completed", task=None, patch=None)
        self.assertEqual(next(iter(built.graph["edges"].values()))["status"], "built")
        failed = architecture.sync(self.graph, plan_of({"core": ("failed", ["api", "db"]),
                                                        "search": ("blocked", ["api"])}),
                                   event="task.merged", task=None, patch=None)
        self.assertEqual(failed.graph["nodes"]["api"]["status"], "failed")

    def test_no_change_keeps_revision(self):
        result = architecture.sync(self.graph, self.plan, event="refresh", task=None, patch=None)
        self.assertFalse(result.changed)
        self.assertEqual(result.graph["revision"], 1)

    def test_valid_patch(self):
        patch = {"add_nodes": [{"id": "cache", "name": "Cache", "kind": "cache", "tech": "Redis"}],
                 "add_edges": [{"from": "api", "to": "cache", "label": "reads", "protocol": "tcp"}],
                 "update_nodes": [{"id": "api", "tech": "FastAPI"}]}
        result = architecture.sync(self.graph, self.plan, event="task.merged", task="core", patch=patch)
        self.assertTrue(result.changed)
        self.assertEqual(result.graph["nodes"]["cache"]["status"], "planned")
        self.assertEqual(result.graph["nodes"]["api"]["tech"], "FastAPI")
        self.assertIn("api--cache", result.graph["edges"])
        self.assertEqual(result.graph["revision"], 2)

    def test_rejected_patch_keeps_graph(self):
        bad_patches = [
            {"add_nodes": [{"id": "x", "kind": "spaceship"}]},
            {"add_nodes": [{"id": "api", "kind": "service"}]},
            {"add_edges": [{"from": "api", "to": "ghost"}]},
            {"update_nodes": [{"id": "ghost", "tech": "x"}]},
            {"remove_edges": ["nope"]},
            {"surprise": []},
            {"add_nodes": [{"id": "ok", "kind": "service"}], "add_edges": [{"from": "ok", "to": "ghost"}]},
        ]
        for patch in bad_patches:
            with self.subTest(patch=patch):
                result = architecture.sync(self.graph, self.plan, event="task.merged", task="core", patch=patch)
                self.assertFalse(result.changed)
                self.assertEqual(result.graph, self.graph)
                self.assertIn("patch rejected", result.summary)

    def test_patch_remove_edge(self):
        edge_id = next(iter(self.graph["edges"]))
        result = architecture.sync(self.graph, self.plan, event="x", task=None, patch={"remove_edges": [edge_id]})
        self.assertEqual(result.graph["edges"], {})


def fake_cli(directory: Path, name: str, script: str) -> None:
    """A fake executable. It appends its argv (one JSON line) and its stdin to <name>.log, then runs `script`."""
    path = directory / name
    path.write_text(f"#!{sys.executable}\nimport json, sys\n"
                    f"stdin = '' if sys.stdin.isatty() else sys.stdin.read()\n"
                    f"open({str(directory / (name + '.log'))!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': stdin}}) + '\\n')\n"
                    + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class WithFakePath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.bin)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def calls(self, name):
        return [json.loads(l) for l in (self.bin / f"{name}.log").read_text().splitlines()]


class ProviderProbeTests(WithFakePath):
    def test_missing_cli(self):
        self.assertEqual(providers.probe("claude").status, "missing")

    def test_claude_logged_in_and_out(self):
        fake_cli(self.bin, "claude", "print(json.dumps({'loggedIn': True, 'authMethod': 'oauth'}))")
        result = providers.probe("claude")
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.calls("claude")[0]["argv"], ["auth", "status"])
        fake_cli(self.bin, "claude", "print(json.dumps({'loggedIn': False}))")
        self.assertEqual(providers.probe("claude").status, "failed")

    def test_codex_status(self):
        fake_cli(self.bin, "codex", "print('Logged in using ChatGPT')")
        self.assertEqual(providers.probe("codex").status, "ok")
        fake_cli(self.bin, "codex", "sys.exit(1)")
        self.assertEqual(providers.probe("codex").status, "failed")

    def test_agy_models_parsed_from_text_and_json(self):
        fake_cli(self.bin, "agy", "print('Available models:\\n  gemini-4-argon-high  Argon\\n- gemini-3.8-flash-low')")
        result = providers.probe("agy")
        self.assertEqual((result.status, result.models), ("ok", ["gemini-4-argon-high", "gemini-3.8-flash-low"]))
        fake_cli(self.bin, "agy", "print(json.dumps({'models': [{'id': 'gemini-4-argon-high'}]}))")
        self.assertEqual(providers.probe("agy").models, ["gemini-4-argon-high"])
        fake_cli(self.bin, "agy", "print('')")
        self.assertEqual(providers.probe("agy").status, "failed")

    def test_simulator_and_endpoint_without_url(self):
        self.assertEqual(providers.probe("simulator").status, "ok")
        with mock.patch.dict(os.environ, {providers.BASE_URL_ENV: ""}):
            self.assertEqual(providers.probe("openai-compatible").status, "missing")

    def test_endpoint_probe_lists_models(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"data": [{"id": "local-a"}]}).encode()
        with mock.patch.dict(os.environ, {providers.BASE_URL_ENV: "http://127.0.0.1:1/v1"}), \
                mock.patch("urllib.request.urlopen", return_value=Response()):
            result = providers.probe("openai-compatible")
        self.assertEqual((result.status, result.models), ("ok", ["local-a"]))

    def test_blank_account_and_catalog(self):
        account = providers.blank_account("codex")
        self.assertEqual((account["enabled"], account["status"], account["checked"], account["models"]),
                         (False, "unknown", None, []))
        self.assertIn("claude", providers.catalog()["providers"])


class CompleterTests(WithFakePath):
    def state(self, *enabled):
        return {"config": {"profile": "balanced", "rigor": "high"},
                "accounts": {n: {"enabled": True, "status": "ok"} for n in enabled}}

    def test_claude_print_mode_and_usage(self):
        fake_cli(self.bin, "claude", "print(json.dumps({'result': 'hello', 'usage': {'input_tokens': 7, "
                                     "'output_tokens': 3}, 'total_cost_usd': 0.01}))")
        complete = providers.completer(lambda: self.state("claude"))
        out = complete(Prompt(system="S", user="U", role="plan", expect_json=True))
        self.assertEqual((out.text, out.provider, out.tokens_in, out.tokens_out, out.cost_usd),
                         ("hello", "claude", 7, 3, 0.01))
        call = self.calls("claude")[0]
        self.assertEqual(call["argv"], ["-p", "--output-format", "json", "--model", out.model])
        self.assertIn("U", call["stdin"])

    def test_unreported_usage_stays_none(self):
        fake_cli(self.bin, "claude", "print(json.dumps({'result': 'x'}))")
        out = providers.completer(lambda: self.state("claude"))(Prompt(system="S", user="U"))
        self.assertIsNone(out.tokens_in)
        self.assertIsNone(out.cost_usd)

    def test_falls_back_to_next_provider_then_raises(self):
        fake_cli(self.bin, "claude", "sys.exit(2)")
        fake_cli(self.bin, "codex", "print(json.dumps({'type': 'item.completed', 'item': "
                                    "{'type': 'agent_message', 'text': 'from codex'}}))")
        complete = providers.completer(lambda: self.state("claude", "codex"))
        out = complete(Prompt(system="S", user="U"))
        self.assertEqual((out.text, out.provider), ("from codex", "codex"))
        self.assertEqual(self.calls("codex")[0]["argv"][:3], ["exec", "--json", "-m"])
        fake_cli(self.bin, "codex", "sys.exit(3)")
        with self.assertRaises(CompletionError):
            complete(Prompt(system="S", user="U"))

    def test_agy_prompt_is_the_argument(self):
        fake_cli(self.bin, "agy", "print(json.dumps({'result': 'g'}))")
        out = providers.completer(lambda: self.state("agy"))(Prompt(system="S", user="U"))
        argv = self.calls("agy")[0]["argv"]
        self.assertEqual((out.text, argv[:1], "--output-format" in argv), ("g", ["-p"], True))

    def test_nothing_enabled_raises(self):
        with self.assertRaises(CompletionError):
            providers.completer(lambda: self.state())(Prompt(system="S", user="U"))


class SecretsTests(WithFakePath):
    VALUE = "s3cret-VALUE-123"

    def test_memory_backend(self):
        self.assertEqual(secrets.backend(), "memory")
        secrets.set("k", self.VALUE)
        self.assertEqual(secrets.get("k"), self.VALUE)
        self.assertIsNone(secrets.get("missing"))
        self.assertIn("memory", providers.store_key("k", self.VALUE))

    def test_value_never_on_argv(self):
        for tool, platform in (("secret-tool", "linux"), ("security", "darwin")):
            with self.subTest(tool=tool):
                fake_cli(self.bin, tool, "")
                with mock.patch.object(secrets.sys, "platform", platform):
                    self.assertNotEqual(secrets.backend(), "memory")
                    note = providers.store_key("claude", self.VALUE)
                call = self.calls(tool)[-1]
                self.assertNotIn(self.VALUE, json.dumps(call["argv"]))
                self.assertIn(self.VALUE, call["stdin"])
                self.assertNotIn(self.VALUE, note)
                (self.bin / tool).unlink()


    # finding 11: a keychain that fails falls back to memory, never a 500
    def test_failing_keychain_falls_back_to_memory(self):
        self.addCleanup(secrets._memory.clear)
        fake_cli(self.bin, "secret-tool", "sys.exit(1)")
        with mock.patch.object(secrets.sys, "platform", "linux"):
            self.assertEqual(secrets.backend(), "keychain-linux")
            note = providers.store_key("claude", self.VALUE)
            self.assertIn("memory for this run only", note)
            self.assertEqual(secrets.get("claude"), self.VALUE)
            for error in (OSError("no dbus"), __import__("subprocess").TimeoutExpired("secret-tool", 10)):
                with self.subTest(error=type(error).__name__), mock.patch.object(secrets, "_run", side_effect=error):
                    self.assertIn("memory for this run only", providers.store_key("codex", self.VALUE))
                    self.assertEqual(secrets.get("codex"), self.VALUE)

    def test_account_set_with_a_failing_keychain_is_accepted(self):
        from jbi import protocol
        from jbi.engine import Engine
        from jbi.store import Store, now
        self.addCleanup(secrets._memory.clear)
        fake_cli(self.bin, "secret-tool", "sys.exit(1)")
        store = Store(self.bin / "run.db", protocol.initial_state("r", now(), name="t", mode="new", target="",
                                                                  max_sessions=1, lanes=1, tmux=""))
        self.addCleanup(store.close)
        with mock.patch.object(secrets.sys, "platform", "linux"):
            Engine(store, None, None).submit({"id": "k", "type": "account.set", "payload": {
                "provider": "claude", "enabled": True, "api_key": self.VALUE}})
        self.assertIn("memory for this run only", store.read("/accounts/claude/key"))
        self.assertNotIn(self.VALUE, json.dumps(store.events_after(0, 1000)))

    def test_child_key_env_names_each_provider_variable(self):
        self.addCleanup(secrets._memory.clear)
        self.assertEqual(providers.child_key_env("claude"), {})
        for name, variable in (("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY"),
                               ("agy", "GEMINI_API_KEY")):
            secrets.set(name, f"key-{name}")
            self.assertEqual(providers.child_key_env(name), {variable: f"key-{name}"})
        secrets.set("openai-compatible", "key-http")
        self.assertEqual(providers.child_key_env("openai-compatible"), {})

    def test_stored_key_reaches_the_provider_cli(self):
        self.addCleanup(secrets._memory.clear)
        fake_cli(self.bin, "claude", "import os\nprint(json.dumps({'result': os.environ.get('ANTHROPIC_API_KEY', 'none')}))")
        state = {"config": {"profile": "balanced", "rigor": "high"},
                 "accounts": {"claude": {"enabled": True, "status": "ok"}}}
        complete = providers.completer(lambda: state)
        self.assertEqual(complete(Prompt(system="S", user="U")).text, "none")
        secrets.set("claude", self.VALUE)
        self.assertEqual(complete(Prompt(system="S", user="U")).text, self.VALUE)
        self.assertNotIn(self.VALUE, json.dumps(self.calls("claude")[-1]["argv"]))


class LogSafetyTests(WithFakePath):
    """Finding 10: provider stderr, stdout and exception text never reach an event or log line."""

    LEAK = "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET"

    def test_completion_error_carries_only_provider_and_exit_code(self):
        fake_cli(self.bin, "claude", f"sys.stderr.write('401 Authorization: Bearer {self.LEAK}\\n'); sys.exit(7)")
        fake_cli(self.bin, "codex", f"print('not json {self.LEAK}')")
        log = self.bin / "debug.log"
        state = {"config": {"profile": "balanced", "rigor": "high"},
                 "accounts": {n: {"enabled": True, "status": "ok"} for n in ("claude", "codex")}}
        with self.assertRaises(CompletionError) as caught:
            providers.completer(lambda: state, debug_log=log)(Prompt(system="S", user="U", purpose="planner.chat"))
        message = str(caught.exception)
        self.assertNotIn(self.LEAK, message)
        self.assertNotIn("Bearer", message)
        self.assertIn("claude: exit 7. The CLI call failed.", message)
        self.assertIn("codex: the reply was not usable (ValueError).", message)
        self.assertEqual(os.stat(log).st_mode & 0o777, 0o600)
        text = log.read_text()
        self.assertIn("exit 7", text)
        self.assertNotIn(self.LEAK, text)
        self.assertNotIn("SECRETSECRET", text)

    def test_redact_patterns(self):
        for raw in ("sk-abc", "ghp_abc", "Bearer abc", "bearer   abc", "api_key=abc", "API-KEY: abc", "apikey=abc",
                    "x" * 32):
            with self.subTest(raw=raw):
                self.assertNotIn("abc" if "abc" in raw else raw, providers.redact(f"before {raw} after"))
        self.assertEqual(providers.redact("exit 2: not found"), "exit 2: not found")

    def test_probe_detail_holds_no_cli_output(self):
        fake_cli(self.bin, "codex", f"print('Logged in with key {self.LEAK}')")
        result = providers.probe("codex")
        self.assertEqual((result.status, result.detail), ("ok", "Signed in"))


class BootstrapTests(unittest.TestCase):
    def state(self):
        return {"run": {"target": "/tmp/app", "mode": "new", "name": "app"},
                "stack": {"layers": [{"layer": "language", "choice": "Python 3.12", "reason": "simple",
                                      "cost": "free"}]},
                "plan": {"prd": {"name": "app"}}}

    def test_brief(self):
        text = bootstrap.brief(self.state())
        self.assertLessEqual(len(text.splitlines()), 100)
        for word in ("/tmp/app", "Python 3.12", ".jbi/commands.json", "format_check", "NO feature code"):
            self.assertIn(word.lower(), text.lower())
        existing = self.state()
        existing["run"]["mode"] = "existing"
        self.assertNotIn("git init", bootstrap.brief(existing))

    def write(self, root: Path, commands) -> None:
        (root / ".jbi").mkdir(exist_ok=True)
        (root / ".jbi" / "commands.json").write_text(json.dumps(commands))

    def test_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(bootstrap.check(root)[0])
            self.write(root, {"build": "true", "lint": "true"})
            self.assertTrue(bootstrap.check(root)[0])
            self.write(root, {"build": "true", "lint": "echo bad lint; exit 3"})
            ok, detail = bootstrap.check(root)
            self.assertFalse(ok)
            self.assertIn("lint exited 3", detail)
            self.write(root, {"build": "true"})               # SPEC 6: build AND lint must exit 0
            ok, detail = bootstrap.check(root)
            self.assertFalse(ok)
            self.assertIn("lint", detail)
            (root / ".jbi" / "commands.json").write_text("{broken")
            self.assertFalse(bootstrap.check(root)[0])


class SkillTests(unittest.TestCase):
    NAMES = ("brief-task", "verify-outside", "review-diff", "security-scan", "architecture-update",
             "bootstrap-repo", "ci-lane", "only-latest", "board-truth")

    def test_every_skill(self):
        for name in self.NAMES:
            with self.subTest(skill=name):
                text = (ROOT / "skills" / name / "SKILL.md").read_text()
                self.assertLess(len(text.splitlines()), 80)
                match = re.match(r"^---\nname: (.+)\ndescription: (.+)\n---\n", text)
                self.assertIsNotNone(match, "frontmatter with name and description")
                self.assertEqual(match.group(1), name)
                for pattern in NEUTRAL:
                    self.assertIsNone(re.search(pattern, text), pattern)
                for word in PRIVATE:
                    self.assertIsNone(re.search(rf"\b{re.escape(word)}\b", text, re.I), "a private term")

    def test_no_unlisted_skill(self):
        found = sorted(p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual(found, sorted(self.NAMES))


if __name__ == "__main__":
    unittest.main()
