"""Regression tests for the 2026-11-24 jbi deep-fix pass.

Covers:
1. Agent dispatch provider routing (claude/agy) and real stderr error propagation.
2. Live dispatch applying --cascade founder and cascading across cooled rungs.
3. Cascade-driven provider enablement (including claude rung firing first).
4. Tracker writer survival under a repeated failure storm and unbacked running -> lost.
5. Orphan worktree and jbi/* branch reclaim on fresh start (including SIGKILL corpse).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_mux_lite import credentials
from jbi import agents, briefs, cli, models, protocol, resume, rigor, shim, waves, worktrees as wt
from jbi.store import Changes, Store
from jbi.supervisor import Supervisor
from jbi.tracking import Tracking

from tests.test_lite_process import FakeEndpoint, SECRET, final, write_call

ROOT = Path(__file__).resolve().parent.parent


def _make_repo(tmp: Path, name: str = "repo") -> tuple[Path, str]:
    repo = tmp / name
    repo.mkdir(parents=True, exist_ok=True)
    wt.init_repo(repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    base = wt.commit_all(repo, "base")
    assert base is not None
    return repo, base


class _NoopHooks:
    def bootstrap_finished(self, *args):
        pass

    task_merged = wave_completed = all_merged = stopped = bootstrap_finished


class Defect1DispatchRoutingAndStderr(unittest.TestCase):
    """Defect 1: provider dispatch routes cleanly and never hides stderr behind 'reason none'."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-d1-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.base = _make_repo(self.tmp)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def test_claude_provider_uses_anthropic_env_and_oauth_file_without_leaking_to_child(self):
        cfg_dir = self.tmp / "claude-cfg"
        cfg_dir.mkdir()
        oauth_secret = "sk-ant-oat01-fake-oauth-token-998877"
        cred_file = cfg_dir / ".credentials.json"
        cred_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": oauth_secret}}))
        os.chmod(cred_file, 0o600)

        with FakeEndpoint([final("BRIEF T1 | uncommitted | PASS")]) as ep:
            source = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(self.tmp),
                "ANTHROPIC_BASE_URL": ep.url,
                "ANTHROPIC_CUSTOM_HEADERS": "X-Proxy-Token: proxy-secret-4411",
                "CLAUDE_CONFIG_DIR": str(cfg_dir),
            }
            spec = {
                "cmd": [sys.executable, "-m", "claude_mux_lite", "/x/spec.json"],
                "provider": "claude",
                "env": {"PYTHONPATH": str(ROOT)},
            }
            argv, env = shim.neutral_launch(spec, source)
            self.assertEqual(argv[1:], ["-m", "claude_mux_lite", "--bootstrap", "/x/spec.json"])
            self.assertEqual(env["CLAUDE_MUX_PROVIDER"], "claude")
            self.assertEqual(env["ANTHROPIC_BASE_URL"], ep.url)
            self.assertEqual(env["ANTHROPIC_CUSTOM_HEADERS"], "X-Proxy-Token: proxy-secret-4411")

            key, headers, upstream = credentials.take_from_environment(env)
            self.assertEqual(upstream, ep.url)
            self.assertEqual(key, oauth_secret)
            self.assertEqual(headers.get("X-Proxy-Token"), "proxy-secret-4411")
            self.assertEqual(headers.get("anthropic-beta"), "oauth-2025-04-20")

    def test_agy_provider_dispatches_through_structured_cli_bridge_inside_fence(self):
        fake_bin = self.tmp / "bin"
        fake_bin.mkdir()
        agy_log = self.tmp / "agy-calls.jsonl"
        fake_agy = fake_bin / "agy"
        fake_agy.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "argv = sys.argv[1:]\n"
            f"with open({str(agy_log)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(argv) + '\\n')\n"
            "model = argv[argv.index('--model') + 1] if '--model' in argv else ''\n"
            "if '--conversation' not in argv:\n"
            "    out = {'conversation_id': 'conv-1', 'status': 'SUCCESS',\n"
            "           'structured_output': {'text': 'writing file', 'tool_calls': [\n"
            "               {'name': 'write_file', 'input': {'path': 'src/a.txt', 'content': 'from-agy\\n'}}]},\n"
            "           'usage': {'input_tokens': 100, 'output_tokens': 25, 'cache_read_tokens': 10}}\n"
            "else:\n"
            "    out = {'conversation_id': 'conv-1', 'status': 'SUCCESS',\n"
            "           'structured_output': {'text': 'BRIEF T1 | uncommitted | PASS\\nDID: wrote src/a.txt',\n"
            "                                 'tool_calls': []},\n"
            "           'usage': {'input_tokens': 40, 'output_tokens': 12, 'cache_read_tokens': 30}}\n"
            "print(json.dumps(out))\n",
            encoding="utf-8",
        )
        fake_agy.chmod(0o755)

        state = protocol.initial_state("r-d1", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["config"]["rigor"] = "vibe"
        state["config"]["provider_cascade"] = [{"provider": "agy", "model": "gemini-4-neon-high"}]
        state["accounts"] = {"agy": {"label": "Google Antigravity", "enabled": True, "status": "ok"}}
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Write src/a.txt", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "test -f src/a.txt"},
        ], 1)
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        sup = Supervisor(store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)
        worktree = wt.create_worktree(self.repo, "T1", self.base, self.run_dir / "worktrees")

        clean_env = {"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}", "HOME": str(self.tmp)}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            ok, detail, model = sup._agent_stage(
                store.read("/plan/tasks/T1"), rigor.stage("vibe", "implement"), worktree, [])
        self.assertTrue(ok, detail)
        self.assertEqual(model, "gemini-4-neon-high")
        self.assertEqual((worktree / "src/a.txt").read_text(), "from-agy\n")
        calls = [json.loads(line) for line in agy_log.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertIn("gemini-4-neon-high", calls[0])
        self.assertIn("--conversation", calls[1])

    def test_failed_dispatch_reports_real_stderr_excerpt_never_reason_none(self):
        state = protocol.initial_state("r-d1-err", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["config"]["rigor"] = "vibe"
        state["accounts"] = {"claude": {"label": "Claude", "enabled": True, "status": "ok"}}
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Write src/a.txt", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "true",
             "override": {"pin": {"provider": "claude", "model": "claude-opus-5-5"}}},
        ], 1)
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        sup = Supervisor(store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)
        worktree = wt.create_worktree(self.repo, "T1", self.base, self.run_dir / "worktrees")

        with mock.patch.dict(os.environ, {"CLAUDE_MUX_BASE_URL": "http://127.0.0.1:1",
                                          "CLAUDE_MUX_API_KEY": SECRET}):
            ok, detail, model = sup._agent_stage(
                store.read("/plan/tasks/T1"), rigor.stage("vibe", "implement"), worktree, [])
        self.assertFalse(ok)
        self.assertEqual(model, "claude-opus-5-5")
        self.assertNotIn("reason none", detail.lower())
        self.assertIn("transport failure", detail.lower())


class Defect2CascadeReachesDispatch(unittest.TestCase):
    """Defect 2: --cascade founder reaches the actual dispatched agent and cascades across rungs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-d2-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.base = _make_repo(self.tmp)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def test_dispatch_applies_founder_cascade_over_stale_dag_prefer(self):
        state = protocol.initial_state("r-d2-a", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["config"]["rigor"] = "vibe"
        state["accounts"] = {
            "claude": {"label": "Claude", "enabled": True, "status": "ok"},
            "agy": {"label": "Google Antigravity", "enabled": True, "status": "ok"},
        }
        # Exact stale override.prefer shape found in wave4-execution-dag.json
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Write src/a.txt", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "true",
             "override": {"prefer": [{"provider": "agy", "model": "gemini-4-argon-high"},
                                     {"provider": "claude", "model": "claude-opus-5-5"}]}},
        ], 1)
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        cli.install_cascade(store, "founder")
        sup = Supervisor(store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)
        worktree = wt.create_worktree(self.repo, "T1", self.base, self.run_dir / "worktrees")

        call = write_call()
        call["content"][0]["input"] = {"path": "src/a.txt", "content": "ok\n"}
        with FakeEndpoint([call, final("BRIEF T1 | uncommitted | PASS")]) as ep:
            with mock.patch.dict(os.environ, {"CLAUDE_MUX_BASE_URL": ep.url, "CLAUDE_MUX_API_KEY": SECRET}):
                ok, detail, model = sup._agent_stage(
                    store.read("/plan/tasks/T1"), rigor.stage("vibe", "implement"), worktree, [])
        self.assertTrue(ok, detail)
        self.assertEqual(model, "claude-opus-5-5")
        self.assertEqual([r["body"]["model"] for r in ep.requests], ["claude-opus-5-5", "claude-opus-5-5"])
        self.assertNotIn("gemini-4-argon-high", [r["body"]["model"] for r in ep.requests])
        self.assertEqual(store.read("/sessions/s-T1-implement-1/model"), "claude-opus-5-5")
        self.assertEqual(store.read("/sessions/s-T1-implement-1/provider"), "claude")

    def test_dispatch_cascades_opus_to_neon_to_carbon_on_rate_limit(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        seen_models: list[str] = []

        class CascadeServer(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model = body["model"]
                seen_models.append(model)
                if model in ("claude-opus-5-5", "gemini-4-neon-high"):
                    err = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "429"}}).encode()
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return
                ans = json.dumps(final("BRIEF T1 | uncommitted | PASS")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(ans)))
                self.end_headers()
                self.wfile.write(ans)

            def log_message(self, *args):
                pass

        srv = HTTPServer(("127.0.0.1", 0), CascadeServer)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), t.join(5)))

        state = protocol.initial_state("r-d2-b", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["config"]["rigor"] = "vibe"
        state["accounts"] = {
            "claude": {"label": "Claude", "enabled": True, "status": "ok"},
            "agy": {"label": "Google Antigravity", "enabled": True, "status": "ok"},
        }
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Do task", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "true"},
        ], 1)
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        cli.install_cascade(store, "founder")
        sup = Supervisor(store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)
        worktree = wt.create_worktree(self.repo, "T1", self.base, self.run_dir / "worktrees")

        dispatched: list[str] = []
        with mock.patch.dict(os.environ, {"CLAUDE_MUX_BASE_URL": url, "CLAUDE_MUX_API_KEY": SECRET}):
            for attempt in range(3):
                store.commit("t", Changes().set("plan", "tasks", "T1", "attempts", attempt), {})
                ok, detail, model = sup._agent_stage(
                    store.read("/plan/tasks/T1"), rigor.stage("vibe", "implement"), worktree, [])
                dispatched.append(model)
                if ok:
                    break
        self.assertTrue(ok, detail)
        self.assertEqual(dispatched, ["claude-opus-5-5", "gemini-4-neon-high", "gemini-4-carbon-high"])
        self.assertNotIn("gemini-4-argon-high", seen_models)


class Defect3CascadeEnablesClaude(unittest.TestCase):
    """Defect 3: a cascade-named provider is enabled so the claude rung can fire."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-d3-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.base = _make_repo(self.tmp)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def test_install_cascade_enables_disabled_claude_and_fires_opus_rung(self):
        state = protocol.initial_state("r-d3-a", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["config"]["rigor"] = "vibe"
        state["accounts"] = {
            "claude": {"label": "Claude", "enabled": False, "status": "unknown", "detail": "", "checked": None, "models": []},
            "agy": {"label": "Google Antigravity", "enabled": True, "status": "unknown", "detail": "", "checked": None, "models": []},
            "simulator": {"label": "Offline simulator", "enabled": True, "status": "ok", "detail": "", "checked": None, "models": []},
        }
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Do task", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "true"},
        ], 1)
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        self.assertFalse(store.read("/accounts/claude/enabled"))
        cli.install_cascade(store, "founder")
        self.assertTrue(store.read("/accounts/claude/enabled"))
        self.assertTrue(store.read("/accounts/agy/enabled"))

        sup = Supervisor(store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)
        worktree = wt.create_worktree(self.repo, "T1", self.base, self.run_dir / "worktrees")
        with FakeEndpoint([final("BRIEF T1 | uncommitted | PASS")]) as ep:
            with mock.patch.dict(os.environ, {"CLAUDE_MUX_BASE_URL": ep.url, "CLAUDE_MUX_API_KEY": SECRET}):
                ok, detail, model = sup._agent_stage(
                    store.read("/plan/tasks/T1"), rigor.stage("vibe", "implement"), worktree, [])
        self.assertTrue(ok, detail)
        self.assertEqual(model, "claude-opus-5-5")
        self.assertEqual(ep.requests[0]["body"]["model"], "claude-opus-5-5")

    def test_install_cascade_respects_offline_and_dry_run(self):
        state = protocol.initial_state("r-d3-b", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["accounts"] = {
            "claude": {"label": "Claude", "enabled": False, "status": "unknown", "detail": "", "checked": None, "models": []},
            "simulator": {"label": "Offline simulator", "enabled": True, "status": "ok", "detail": "", "checked": None, "models": []},
        }
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        cli.install_cascade(store, "founder", offline=True)
        self.assertFalse(store.read("/accounts/claude/enabled"))

    def test_mixed_case_cascade_provider_enables_catalog_account(self):
        state = protocol.initial_state("r-d3-c", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["accounts"] = {
            "claude": {"label": "Claude", "enabled": False, "status": "unknown", "detail": "", "checked": None, "models": []},
        }
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        cli.install_cascade(store, '[{"provider": "Claude", "model": "claude-opus-5-5"}]')
        self.assertTrue(store.read("/accounts/claude/enabled"))
        choice = models.select(store.snapshot()[1], "implement")
        self.assertEqual((choice.provider, choice.model), ("claude", "claude-opus-5-5"))


class Defect4TrackerStormAndLiveness(unittest.TestCase):
    """Defect 4: failure storms keep events flowing and unbacked running sessions become lost."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-d4-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.base = _make_repo(self.tmp)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def test_repeated_failure_storm_keeps_events_flowing_and_reaches_failed(self):
        import sqlite3
        state = protocol.initial_state("r-d4-storm", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["config"]["rigor"] = "vibe"
        state["accounts"] = {"claude": {"label": "Claude", "enabled": True, "status": "ok"}}
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Fail repeatedly", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "true",
             "override": {"pin": {"provider": "claude", "model": "claude-opus-5-5"}}},
        ], 1)
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        sup = Supervisor(store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)
        worktree = wt.create_worktree(self.repo, "T1", self.base, self.run_dir / "worktrees")

        # Inject transient SQLite locked errors into Store._retry_db's path via wrapper
        orig_retry = store._retry_db
        flaky_count = {"n": 0}
        def flaky_retry(fn):
            def wrapped():
                flaky_count["n"] += 1
                if flaky_count["n"] % 7 == 0:
                    raise sqlite3.OperationalError("database is locked")
                return fn()
            return orig_retry(wrapped)
        store._retry_db = flaky_retry

        # Run 8 consecutive failures (2 full DECISION_AFTER cycles) under failure storm
        seqs = [store.seq]
        with mock.patch.dict(os.environ, {"CLAUDE_MUX_BASE_URL": "http://127.0.0.1:1",
                                          "CLAUDE_MUX_API_KEY": SECRET}):
            for attempt in range(8):
                store.commit("test.attempt", Changes().set("plan", "tasks", "T1", "attempts", attempt), {})
                ok, detail, _model = sup._agent_stage(
                    store.read("/plan/tasks/T1"), rigor.stage("vibe", "implement"), worktree, [])
                self.assertFalse(ok)
                self.assertNotIn("reason none", detail.lower())
                self.assertGreater(store.seq, seqs[-1], "event seq stopped advancing during failure storm")
                seqs.append(store.seq)

        # Every session in run.db reached 'failed' (none stuck 'running')
        sessions = store.read("/sessions") or {}
        self.assertEqual(len(sessions), 8)
        for sid, s in sessions.items():
            self.assertEqual(s["status"], "failed", f"{sid} stayed {s['status']}")
        # fleet.db lease session is NOT stuck 'busy'
        fleet_sessions = {s["id"]: s["status"] for s in sup.tracking.fleet()["sessions"]}
        self.assertNotIn("busy", fleet_sessions.values(), f"fleet.db left busy session: {fleet_sessions}")
        # Events table is gapless from 1..store.seq
        events = store.events_after(0, 1000)
        self.assertEqual([e["seq"] for e in events], list(range(1, store.seq + 1)))

    def test_unbacked_running_session_becomes_lost_on_reconcile_and_resume(self):
        state = protocol.initial_state("r-d4-lost", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Do task", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "true"},
        ], 1)
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        store.commit("session.status", Changes().set("sessions", "s-ghost-1", {
            "task": "T1", "role": "implement", "provider": "agy", "model": "gemini-4-neon-high",
            "status": "running", "started": "2026-11-24T00:00:00Z",
        }), {})
        sup = Supervisor(store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)

        reconciled = sup.reconcile_sessions()
        self.assertEqual(reconciled, ["s-ghost-1"])
        self.assertEqual(store.read("/sessions/s-ghost-1/status"), "lost")

        # Also verify prepare_resume transitions stale running (run.db) and busy (fleet.db) to lost
        store.commit("session.status", Changes().set("sessions", "s-ghost-2", {
            "task": "T1", "role": "implement", "provider": "agy", "model": "gemini-4-neon-high",
            "status": "running", "started": "2026-11-24T00:00:00Z",
        }), {})
        worktree = wt.create_worktree(self.repo, "T1", self.base, self.run_dir / "worktrees")
        sup._task_lease("T1", worktree)
        self.assertIn("busy", [s["status"] for s in sup.tracking.fleet()["sessions"]])
        cli.prepare_resume(store)
        self.assertEqual(store.read("/sessions/s-ghost-2/status"), "lost")
        self.assertNotIn("busy", [s["status"] for s in sup.tracking.fleet()["sessions"]])

    def test_scheduler_survives_admit_exception_and_keeps_running(self):
        import threading
        state = protocol.initial_state("r-d4-sched", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        sup = Supervisor(store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)
        calls = {"n": 0}
        second = threading.Event()
        def flaky_admit():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient storm error")
            second.set()
            sup._stopping = "quit"
        sup._admit = flaky_admit
        sup.start_execution()
        self.assertTrue(second.wait(5), "scheduler thread died on first _admit exception")
        self.assertGreaterEqual(calls["n"], 2)


class Defect5StartReclaimOrphans(unittest.TestCase):
    """Defect 5: dead worktrees and orphan jbi/* branches are reclaimed at START."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-d5-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.base = _make_repo(self.tmp)

    def _dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(5)
        return proc.pid

    def test_corpse_worktree_holding_jbi_branch_does_not_block_fresh_start(self):
        dead_run = self.tmp / "runs" / "r-dead-1"
        dead_run.mkdir(parents=True)
        (dead_run / "control.json").write_text(json.dumps({
            "url": "http://127.0.0.1:0", "token": "x", "run_id": "r-dead-1", "pid": self._dead_pid(),
        }))
        corpse = wt.create_worktree(self.repo, "T1", self.base, dead_run / "worktrees")
        self.assertTrue(corpse.is_dir())
        self.assertIsNotNone(wt.head(self.repo, "jbi/T1"))

        # Fresh run targeting the same task T1 must reclaim the corpse before dispatch
        fresh_run = self.tmp / "runs" / "r-fresh-2"
        fresh_run.mkdir(parents=True)
        state = protocol.initial_state("r-fresh-2", "now", name="demo", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["config"]["rigor"] = "vibe"
        state["accounts"] = {"simulator": {"label": "Offline simulator", "enabled": True, "status": "ok"}}
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Create file", "deps": [],
             "owns": ["src/t1.txt"], "acceptance": "true"},
        ], 1)
        store = Store(fresh_run / "run.db", state)
        self.addCleanup(store.close)
        sup = Supervisor(store, _NoopHooks(), fresh_run, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)
        # Stop the scheduler before it starts, so no dispatch races this test's
        # own worktree create below. Reclaim runs before the scheduler thread
        # starts, so it still fires.
        sup._stopping = "quit"
        sup.start_execution()

        self.assertFalse(corpse.exists(), "corpse worktree survived fresh start")
        self.assertIsNone(wt.head(self.repo, "jbi/T1"), "orphan jbi/T1 branch survived fresh start")
        # Creating fresh worktree for T1 now succeeds cleanly without 'already used by worktree'
        new_wt = wt.create_worktree(self.repo, "T1", self.base, fresh_run / "worktrees")
        self.assertTrue((new_wt / ".git").is_file())

    def test_sigkill_supervisor_leaves_zero_registrations_and_zero_jbi_branches_after_reclaim(self):
        import signal
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()) if holder.poll() is None else None)

        dead_run = self.tmp / "runs" / "r-killed"
        dead_run.mkdir(parents=True)
        (dead_run / "control.json").write_text(json.dumps({
            "url": "http://127.0.0.1:0", "token": "x", "run_id": "r-killed", "pid": holder.pid,
        }))
        wt1 = wt.create_worktree(self.repo, "X1", self.base, dead_run / "worktrees")
        wt2 = wt.create_worktree(self.repo, "X2", self.base, dead_run / "worktrees")
        # Also create a parked ref (must survive!) and a non-jbi user worktree (must survive!)
        wt.git(self.repo, "branch", "jbi-parked/keep-me", self.base)
        user_wt = self.tmp / "user-wt"
        wt.git(self.repo, "worktree", "add", "-q", "-b", "feature/user", str(user_wt), self.base)

        # SIGKILL the simulated supervisor and reap
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(5)

        next_run = self.tmp / "runs" / "r-next"
        next_run.mkdir(parents=True)
        report = resume.reclaim_start_orphans(self.repo, current_run_dir=next_run, runs_root=self.tmp / "runs")
        self.assertEqual(sorted(report.branches_removed), ["jbi/X1", "jbi/X2"])
        self.assertFalse(wt1.exists())
        self.assertFalse(wt2.exists())

        # Zero jbi/* branches remain and zero jbi worktree registrations remain
        remaining_jbi_branches = [b for b in wt.git(
            self.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/jbi/", check=False).splitlines() if b]
        self.assertEqual(remaining_jbi_branches, [])
        remaining_wts = [e["path"] for e in resume._list_repo_worktrees(self.repo)]
        self.assertNotIn(str(wt1), remaining_wts)
        self.assertNotIn(str(wt2), remaining_wts)
        # User worktree and parked ref survived untouched
        self.assertTrue(user_wt.is_dir())
        self.assertIsNotNone(wt.head(self.repo, "jbi-parked/keep-me"))

    def test_live_run_worktree_and_branch_are_not_reclaimed(self):
        live_run = self.tmp / "runs" / "r-live"
        live_run.mkdir(parents=True)
        (live_run / "control.json").write_text(json.dumps({
            "url": "http://127.0.0.1:0", "token": "x", "run_id": "r-live", "pid": os.getpid(),
        }))
        live_wt = wt.create_worktree(self.repo, "LIVE", self.base, live_run / "worktrees")
        next_run = self.tmp / "runs" / "r-other"
        next_run.mkdir(parents=True)
        report = resume.reclaim_start_orphans(self.repo, current_run_dir=next_run, runs_root=self.tmp / "runs")
        self.assertEqual(report.worktrees_removed, [])
        self.assertEqual(report.branches_removed, [])
        self.assertTrue(live_wt.is_dir())
        self.assertIsNotNone(wt.head(self.repo, "jbi/LIVE"))


class ReviewFixes(unittest.TestCase):
    """The adversarial-review fixes: cooling scope, argv bounds, codex, commit rollback, resume guard."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-rf-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.base = _make_repo(self.tmp)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def _supervisor(self, store):
        sup = Supervisor(store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)
        return sup

    def test_environment_refusal_keeps_the_lane_healthy(self):
        state = protocol.initial_state("r-rf-b", "now", name="demo", mode="existing",
                                       target=str(self.repo), max_sessions=1, lanes=1, tmux="")
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        sup = self._supervisor(store)
        choice = models.Choice("claude", "claude-opus-5-5", "S")
        session = type("S", (), {"status": {"exit_code": 1, "turns": 0}})()
        detail = ("no valid receipt (exit 1, reason none); agent: "
                  "ERROR: before_run refused: workdir is not clean at its base commit")
        sup._maybe_cool_lane("T1", choice, session, detail)
        self.assertFalse(sup.cooldowns.cooling("claude/claude-opus-5-5"),
                         "an environment refusal cooled a healthy lane")
        transport = type("S", (), {"status": {"exit_code": 1, "turns": 0}})()
        sup._maybe_cool_lane("T1", choice, transport,
                             "no valid receipt (exit 1, reason none); agent: "
                             "ERROR: ProtocolError: transport failure: connection refused")
        self.assertTrue(sup.cooldowns.cooling("claude/claude-opus-5-5"),
                        "a transport failure did not cool the lane")

    def test_oversize_prompt_is_refused_before_the_argv_limit(self):
        bridge = credentials._AgyBridge([])
        self.addCleanup(bridge.close)
        huge = "x" * (credentials._AgyBridge.BLOCK_MAX_CHARS + 5000)
        payload = {"model": "gemini-4-neon-high", "messages": [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u1", "content": huge}]}]}
        flat = credentials._AgyBridge._flatten_content(payload["messages"][0]["content"])
        self.assertLessEqual(len(flat), credentials._AgyBridge.BLOCK_MAX_CHARS + 80)
        self.assertIn("[truncated by the jbi agy bridge]", flat)
        with mock.patch.object(bridge, "_invoke", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"structured_output": {"text": "ok", "tool_calls": []}}), "")):
            status, body, _extra = bridge.handle(json.dumps(payload).encode(), 30.0)
        self.assertEqual(status, 200)          # the per-block cap keeps a normal turn servable

        # A full prompt past the bridge bound is refused with a named error, never E2BIG.
        with mock.patch.object(credentials._AgyBridge, "BLOCK_MAX_CHARS", 10_000_000):
            small = [{"role": "user", "content": [{"type": "text", "text": "y" * 100_001}]}]
            payload_full = {"model": "gemini-4-neon-high", "system": "", "messages": small}
            status, body, _extra = bridge.handle(json.dumps(payload_full).encode(), 30.0)
        self.assertEqual(status, 400)
        self.assertIn("prompt too large", body.decode())

    def test_codex_provider_refuses_with_a_named_error(self):
        with self.assertRaises(credentials.BrokerError) as caught:
            credentials.take_from_environment({"CLAUDE_MUX_PROVIDER": "codex"})
        self.assertIn("codex", str(caught.exception))

    def test_tracking_commit_failure_rolls_back_and_recovers(self):
        tracking = Tracking(self.run_dir / "fleet.db")
        self.addCleanup(tracking.close)
        tracking.create_task(title="t", goal="", owns=["a.txt"], branch="b",
                             worktree=str(self.tmp), base_commit="a" * 40, task_id="RF1")
        real_db = tracking._db

        class _DbProxy:
            """Delegate everything to the real connection; fail COMMIT on demand."""

            def __init__(self, db):
                self._db = db
                self.fail_commit = False

            @property
            def in_transaction(self):
                return self._db.in_transaction

            def execute(self, sql, *args):
                if self.fail_commit and isinstance(sql, str) and sql.strip().upper() == "COMMIT":
                    raise sqlite3.OperationalError("disk I/O error")
                return self._db.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._db, name)

        proxy = _DbProxy(real_db)
        tracking._db = proxy
        proxy.fail_commit = True
        with self.assertRaises(sqlite3.OperationalError):
            with tracking._tx() as db:
                db.execute("UPDATE tasks SET goal='x' WHERE id='RF1'")
        self.assertFalse(proxy.in_transaction, "a failed COMMIT left the transaction open")
        proxy.fail_commit = False
        with tracking._tx() as db:
            db.execute("UPDATE tasks SET goal='recovered' WHERE id='RF1'")
        self.assertEqual(tracking.get_task("RF1")["goal"], "recovered")

    def test_resume_refuses_a_live_run_and_allows_a_dead_one(self):
        live = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)", "jbi-live-probe"])
        self.addCleanup(live.kill)
        self.addCleanup(live.wait)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if b"jbi" in Path(f"/proc/{live.pid}/cmdline").read_bytes():
                    break
            except OSError:
                pass
            time.sleep(0.05)
        self.run_dir.joinpath("control.json").write_text(json.dumps({"pid": live.pid}))
        with self.assertRaises(SystemExit) as caught:
            cli._refuse_resume_of_a_live_run(self.run_dir)
        self.assertIn("is live", str(caught.exception))
        dead = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
        dead.wait()
        self.run_dir.joinpath("control.json").write_text(json.dumps({"pid": dead.pid}))
        cli._refuse_resume_of_a_live_run(self.run_dir)          # a dead owner never blocks

    def test_pin_provider_case_is_normalized(self):
        state = protocol.initial_state("r-rf-c", "now", name="demo", mode="existing",
                                       target=str(self.repo), max_sessions=1, lanes=1, tmux="")
        state["accounts"] = {"claude": {"label": "Claude", "enabled": True, "status": "ok"}}
        store = Store(self.run_dir / "run.db", state)
        self.addCleanup(store.close)
        task = {"id": "T9", "override": {"pin": {"provider": "Claude", "model": "claude-opus-5-5"}}}
        self.assertIsNone(models.blocked(state, task), "a capitalized pin parked the task forever")
        choice = models.select(state, "implement", task=task)
        self.assertEqual((choice.provider, choice.model), ("claude", "claude-opus-5-5"))


if __name__ == "__main__":
    unittest.main()
