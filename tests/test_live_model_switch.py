"""Tests for live model/cascade switching via `model.set` and `jbi model`."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jbi import cli, models, protocol
from jbi.engine import Engine
from jbi.store import Changes, Store


class FakeRuntime:
    def __init__(self) -> None:
        self.cooldowns = models.ProviderCooldowns()
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


class LiveModelSwitchTests(unittest.TestCase):
    def test_parse_cascade_formats_and_validation(self) -> None:
        self.assertEqual(
            models.parse_cascade("claude/claude-opus-5-5,agy:gemini-4-neon-medium"),
            [
                {"provider": "claude", "model": "claude-opus-5-5"},
                {"provider": "agy", "model": "gemini-4-neon-medium"},
            ],
        )
        self.assertEqual(
            models.parse_cascade(json.dumps([{"provider": "claude", "model": "claude-opus-5-5"}])),
            [{"provider": "claude", "model": "claude-opus-5-5"}],
        )
        self.assertEqual(
            models.parse_cascade("founder"),
            [dict(entry) for entry in models.FOUNDER_CASCADE],
        )
        with self.assertRaisesRegex(ValueError, "morph_refused"):
            models.parse_cascade("agy/morph-fast")
        with self.assertRaisesRegex(ValueError, "unknown provider"):
            models.parse_cascade("bogus/model-1")
        with self.assertRaisesRegex(ValueError, "cascade_invalid"):
            models.parse_cascade("claude")

    def test_model_set_updates_cascade_enables_accounts_clears_cooldowns_and_wakes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(
                Path(tmp) / "run.db",
                protocol.initial_state(
                    "r-live", "2026-11-26T00:00:00Z", name="wave5", mode="existing",
                    target=tmp, max_sessions=5, lanes=4, tmux="tmux",
                ),
            )
            store.commit("run.phase", Changes().replace("run", "phase", "execute"))
            runtime = FakeRuntime()
            runtime.cooldowns.mark("claude/claude-opus-5-5", 120.0)
            runtime.cooldowns.mark("claude", 120.0)
            engine = Engine(store, lambda *a, **k: "", runtime=runtime)

            res = engine.submit({
                "id": "cmd-model-1",
                "type": "model.set",
                "payload": {
                    "cascade": "claude/claude-opus-5-5,agy/gemini-4-neon-medium",
                },
            })
            self.assertTrue(res["accepted"])
            self.assertEqual(runtime.wakes, 1)
            self.assertFalse(runtime.cooldowns.cooling("claude/claude-opus-5-5"))
            self.assertFalse(runtime.cooldowns.cooling("claude"))

            state = store.snapshot()[1]
            self.assertEqual(
                state["config"]["provider_cascade"],
                [
                    {"provider": "claude", "model": "claude-opus-5-5"},
                    {"provider": "agy", "model": "gemini-4-neon-medium"},
                ],
            )
            self.assertTrue(state["accounts"]["claude"]["enabled"])
            self.assertTrue(state["accounts"]["agy"]["enabled"])

            choice = models.select(state, "implement", cooldowns=runtime.cooldowns)
            self.assertEqual((choice.provider, choice.model), ("claude", "claude-opus-5-5"))
            store.close()

    def test_cli_model_command_sends_model_set(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.send.return_value = {"protocol": 1, "accepted": True, "id": "cli-1", "seq": 9}
        fake_client.state.return_value = {
            "state": {
                "config": {
                    "provider_cascade": [{"provider": "claude", "model": "claude-opus-5-5"}]
                },
                "accounts": {"claude": {"enabled": True}},
            }
        }
        with mock.patch.object(cli, "run_client", return_value=fake_client):
            rc = cli.main(["model", "claude/claude-opus-5-5"])
        self.assertEqual(rc, 0)
        fake_client.send.assert_called_once_with(
            "model.set",
            {"cascade": "claude/claude-opus-5-5", "clear_cooldowns": True},
        )


if __name__ == "__main__":
    unittest.main()
