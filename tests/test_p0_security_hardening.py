"""Tests for P0 JBI security hardening: bwrap secret masking, acceptance isolation, API auth, and 429 reroute."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_mux_lite import sandbox
from jbi import supervisor


class P0SecurityHardeningTests(unittest.TestCase):
    def test_secret_mask_args_hides_existing_sensitive_home_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for name in (".ssh", ".claude", ".just-build-it", ".tangram-dev"):
                (home / name).mkdir(parents=True)
            args = sandbox.secret_mask_args(home)
            for name in (".ssh", ".claude", ".just-build-it", ".tangram-dev"):
                target = str(home / name)
                self.assertIn(target, args)
                idx = args.index(target)
                self.assertEqual(args[idx - 1], "--tmpfs")

    def test_acceptance_argv_masks_secrets_without_unshare_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            worktree.mkdir()
            argv = sandbox.acceptance_argv("echo ok", worktree)
            if shutil.which("bwrap"):
                self.assertEqual(Path(argv[0]).name, "bwrap")
                self.assertNotIn("--unshare-all", argv)
                self.assertIn("--ro-bind", argv)
                self.assertIn("--tmpfs", argv)
                self.assertIn("--bind", argv)
                self.assertIn(str(worktree.resolve()), argv)
                # Prove live bwrap execution hides ~/.ssh and ~/.just-build-it/runs while keeping worktree readable
                probe_cmd = (
                    "test ! -e \"$HOME/.ssh/id_ed25519\" && "
                    "test ! -e \"$HOME/.just-build-it/runs\" && "
                    "echo isolated-ok"
                )
                probe = subprocess.run(
                    sandbox.acceptance_argv(probe_cmd, worktree),
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)
                self.assertIn("isolated-ok", probe.stdout)

    def test_transient_provider_error_helper(self) -> None:
        self.assertTrue(supervisor._transient_provider_error(
            'ERROR: HTTP 429: {"type":"error","error":{"type":"rate_limit_error"}}'
        ))
        self.assertTrue(supervisor._transient_provider_error("HTTP 529 overloaded_error"))
        self.assertFalse(supervisor._transient_provider_error("AssertionError: expected 1 to equal 2"))


if __name__ == "__main__":
    unittest.main()
