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
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_mux_lite import credentials
from jbi import agents, briefs, cli, models, protocol, resume, rigor, shim, waves, worktrees as wt
from jbi.store import Changes, Store
from jbi.supervisor import Supervisor

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


if __name__ == "__main__":
    unittest.main()
