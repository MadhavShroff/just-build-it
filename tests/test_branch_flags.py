"""Tests for --base/--branch and the founder provider cascade.

Brief w19-jbi-base-branch. Part 1: the base a task cuts from and the branch where
verified work lands are two names, set once at start. Part 2, amended 2026-11-24:
the cascade is two rungs, claude-opus-5-5 then Gemini Neon falling back to Carbon;
no morph model may appear in the catalog or in a cascade override.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from jbi import cli, models, protocol, worktrees as wt
from jbi.store import Changes, Store
from jbi.supervisor import Supervisor

from tests.test_runtime import RunFixture, task

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "jbi" / "data" / "models.json"


def make_repo(tmp: Path, name: str = "repo") -> Path:
    repo = tmp / name
    repo.mkdir(parents=True, exist_ok=True)
    wt.git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("base\n")
    wt.commit_all(repo, "base")
    return repo


def make_store(tmp: Path, repo: Path) -> Store:
    run_dir = tmp / f"run-{uuid.uuid4().hex[:6]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    state = protocol.initial_state(f"t-{uuid.uuid4().hex[:10]}", "now", name="t", mode="existing",
                                   target=str(repo), max_sessions=3, lanes=1, tmux="")
    return Store(run_dir / "run.db", state)


def timeline_text(store: Store) -> str:
    return " ".join(str(row) for row in (store.read("/timeline") or []))


class BranchFlagCli(unittest.TestCase):
    """--base and --branch are validated at start, before any agent runs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-flags-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)
        self.store = make_store(self.tmp, self.repo)
        self.addCleanup(self.store.close)

    def test_help_prints_both_new_options(self):
        done = subprocess.run([sys.executable, "-m", "jbi", "--help"], capture_output=True,
                              text=True, cwd=ROOT, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--base", done.stdout)
        self.assertIn("--branch", done.stdout)

    def test_a_missing_base_refuses_at_start_with_a_named_error(self):
        with self.assertRaises(SystemExit) as caught:
            cli.apply_branch_flags(self.store, self.repo, "ghost", None)
        self.assertIn("base_missing", str(caught.exception))
        self.assertIn("ghost", str(caught.exception))
        self.assertIsNone(self.store.read("/bootstrap/base_branch"),
                          "a refused --base was stored anyway")

    def test_a_commit_sha_as_branch_refuses_with_the_named_reason(self):
        sha = wt.head(self.repo, "main")
        for name in (sha, sha + sha[:24]):          # 40-hex and 64-hex
            with self.assertRaises(SystemExit) as caught:
                cli.apply_branch_flags(self.store, self.repo, "main", name)
            self.assertIn("fast_forward_main needs a branch to advance", str(caught.exception))
            self.assertIn("branch_refused", str(caught.exception))

    def test_a_commit_sha_as_base_refuses_and_names_the_flag(self):
        """A SHA in --base with --branch omitted is the landing branch and must refuse too."""
        sha = wt.head(self.repo, "main")
        for name in (sha, sha + sha[:24]):          # 40-hex and 64-hex
            with self.assertRaises(SystemExit) as caught:
                cli.apply_branch_flags(self.store, self.repo, name, None)
            self.assertIn("branch_refused", str(caught.exception))
            self.assertIn("--base must name a branch", str(caught.exception))
        self.assertIsNone(self.store.read("/bootstrap/base_branch"),
                          "a refused --base was stored anyway")

    def test_a_missing_branch_is_created_from_the_base_and_logged(self):
        cli.apply_branch_flags(self.store, self.repo, "main", "new-landing")
        self.assertEqual(wt.head(self.repo, "new-landing"), wt.head(self.repo, "main"))
        self.assertIn("created branch new-landing from base main", timeline_text(self.store))
        self.assertEqual(self.store.read("/bootstrap/base_branch"), "main")
        self.assertEqual(self.store.read("/bootstrap/branch"), "new-landing")

    def test_an_empty_branch_equals_the_base(self):
        wt.git(self.repo, "branch", "develop")
        cli.apply_branch_flags(self.store, self.repo, "develop", "")
        self.assertEqual(self.store.read("/bootstrap/base_branch"), "develop")
        self.assertEqual(self.store.read("/bootstrap/branch"), "develop")

    def test_a_branch_that_resolves_only_to_a_non_branch_ref_refuses(self):
        """A tag, a short SHA, or HEAD is not a branch: fast_forward_main cannot advance it."""
        sha = wt.head(self.repo, "main")
        wt.git(self.repo, "tag", "v1.0")
        for name in ("v1.0", sha[:8], "HEAD"):
            with self.assertRaises(SystemExit) as caught:
                cli.apply_branch_flags(self.store, self.repo, "main", name)
            self.assertIn("branch_refused", str(caught.exception), name)
            self.assertIn("--branch", str(caught.exception), name)
            self.assertIn("fast_forward_main needs a branch", str(caught.exception), name)
            self.assertIsNone(wt.head(self.repo, f"refs/heads/{name}"),
                              f"{name} silently became a branch")

    def test_a_dry_run_refuses_a_missing_branch_and_creates_nothing(self):
        """--dry-run never touches the repository: a missing --branch refuses instead."""
        with self.assertRaises(SystemExit) as caught:
            cli.apply_branch_flags(self.store, self.repo, "main", "dry-landing", dry_run=True)
        self.assertIn("branch_missing", str(caught.exception))
        self.assertIsNone(wt.head(self.repo, "dry-landing"), "the dry run created the branch")
        self.assertIsNone(self.store.read("/bootstrap/branch"), "the refused branch was stored anyway")
        # An existing landing branch still validates and stores under --dry-run.
        cli.apply_branch_flags(self.store, self.repo, "main", "", dry_run=True)
        self.assertEqual(self.store.read("/bootstrap/branch"), "main")

    def test_the_stored_choice_survives_bootstrap_on_another_branch(self):
        """HAZARD 1: bootstrap runs on the repo's own branch but must not overwrite the flag."""
        wt.git(self.repo, "branch", "develop")
        run_dir = self.tmp / f"run-{uuid.uuid4().hex[:6]}"
        run_dir.mkdir()
        state = protocol.initial_state("t-x", "now", name="t", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["accounts"] = {"simulator": {"label": "Simulator", "enabled": True, "status": "ok"}}
        store = Store(run_dir / "run.db", state)
        self.addCleanup(store.close)
        cli.apply_branch_flags(store, self.repo, "develop", "")
        from tests.test_runtime import Hooks
        hooks = Hooks()
        sup = Supervisor(store, hooks, run_dir, self.repo, use_tmux=False)
        self.addCleanup(lambda: (sup.stop("kill"), hooks.stopped_event.wait(20)))
        self.addCleanup(sup.tracking.close)
        sup._bootstrap_safe()
        # The raw test Hooks carry no engine, so the PASSED proof is the check detail
        # and the hook call, not the status key.
        self.assertIn("build: exit 0; lint: exit 0", store.read("/bootstrap/detail"))
        self.assertIn(("bootstrap", True), hooks.calls)
        self.assertEqual(store.read("/bootstrap/base_branch"), "develop",
                         "bootstrap replaced the named base with the repository's own branch")
        self.assertEqual(store.read("/bootstrap/branch"), "develop")
        self.assertEqual((sup.base_branch, sup.branch), ("develop", "develop"))

    def test_bootstrap_cuts_jbi_bootstrap_from_the_named_base(self):
        """HAZARD: `checkout -b jbi/bootstrap` with no start point cuts from HEAD.

        The lease records base_branch as the base, so jbi/bootstrap must start at
        base_branch, not at the branch the repository happens to sit on. The release
        parks the finished work under refs/heads/jbi-parked/bootstrap-*, and that
        parked ref keeps the proof ancestry."""
        wt.git(self.repo, "branch", "develop")
        wt.git(self.repo, "checkout", "-q", "develop")
        (self.repo / "DEVELOP").write_text("develop\n")
        wt.commit_all(self.repo, "give develop its own commit")
        wt.git(self.repo, "checkout", "-q", "main")
        develop_head = wt.head(self.repo, "develop")
        self.assertNotEqual(develop_head, wt.head(self.repo, "main"),
                            "the fixture must give develop its own commit")
        run_dir = self.tmp / f"run-{uuid.uuid4().hex[:6]}"
        run_dir.mkdir()
        state = protocol.initial_state("t-x", "now", name="t", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["accounts"] = {"simulator": {"label": "Simulator", "enabled": True, "status": "ok"}}
        store = Store(run_dir / "run.db", state)
        self.addCleanup(store.close)
        cli.apply_branch_flags(store, self.repo, "develop", "")
        from tests.test_runtime import Hooks
        hooks = Hooks()
        sup = Supervisor(store, hooks, run_dir, self.repo, use_tmux=False)
        self.addCleanup(lambda: (sup.stop("kill"), hooks.stopped_event.wait(20)))
        self.addCleanup(sup.tracking.close)
        sup._bootstrap_safe()
        self.assertEqual(store.read("/bootstrap/base_branch"), "develop")
        parked = [ref for ref
                  in wt.git(self.repo, "for-each-ref", "--format=%(refname)",
                            "refs/heads/jbi-parked/").splitlines()
                  if ref.removeprefix("refs/heads/jbi-parked/").startswith("bootstrap-")]
        self.assertEqual(len(parked), 1, f"expected one parked bootstrap ref, got {parked}")
        self.assertEqual(wt.git(self.repo, "rev-parse", f"{parked[0]}^"), develop_head,
                         "bootstrap cut jbi/bootstrap from the repository's own branch, not develop")
        self.assertEqual(sup.tracking.get_task("bootstrap")["base_commit"], develop_head,
                         "the lease base is not develop's head")

    def test_bootstrap_refuses_a_missing_named_base_before_any_agent(self):
        from tests.test_runtime import Hooks
        run_dir = self.tmp / f"run-{uuid.uuid4().hex[:6]}"
        run_dir.mkdir()
        state = protocol.initial_state("t-x", "now", name="t", mode="existing", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        store = Store(run_dir / "run.db", state)
        self.addCleanup(store.close)
        store.commit("flags", Changes().set("bootstrap", "base_branch", "ghost")
                     .set("bootstrap", "branch", "ghost"), {})
        hooks = Hooks()
        sup = Supervisor(store, hooks, run_dir, self.repo, use_tmux=False)
        self.addCleanup(lambda: (sup.stop("kill"), hooks.stopped_event.wait(20)))
        self.addCleanup(sup.tracking.close)
        started: list = []
        original = sup.run_session
        sup.run_session = lambda *a, **k: started.append(a) or original(*a, **k)
        sup._bootstrap_safe()
        self.assertEqual(store.read("/bootstrap/status"), "failed")
        self.assertIn("base branch 'ghost' does not exist", store.read("/bootstrap/detail"))
        self.assertEqual(started, [], "bootstrap started an agent although the base branch is missing")
        self.assertIn(("bootstrap", False), hooks.calls)


class BranchFlagRun(unittest.TestCase):
    """The real CLI refuses and creates at start."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-flagrun-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)
        self.home = self.tmp / "home"
        self.dag = self.tmp / "dag.json"
        self.dag.write_text(json.dumps({"tasks": [
            {"id": "a", "title": "A", "goal": "build a", "owns": ["src/a.py"], "acceptance": "true"}]}))
        self.env = {**os.environ, "JBI_HOME": str(self.home)}

    def base_argv(self) -> list[str]:
        return [sys.executable, "-m", "jbi", "--repo", str(self.repo), "--dag", str(self.dag),
                "--offline", "--no-tailscale", "--no-menu", "--registry", str(self.tmp / "reg.json")]

    def test_a_missing_base_refuses_the_whole_run(self):
        done = subprocess.run(self.base_argv() + ["--base", "ghost", "--start"], env=self.env,
                              capture_output=True, text=True, cwd=ROOT, timeout=60)
        self.assertNotEqual(done.returncode, 0, "a missing base must exit non-zero")
        self.assertIn("base_missing", done.stderr)
        self.assertEqual(list(self.home.glob("runs/*/worktrees")), [],
                         "the refusal left task worktrees behind")

    def test_a_sha_branch_refuses_the_whole_run(self):
        sha = wt.head(self.repo, "main")
        done = subprocess.run(self.base_argv() + ["--branch", sha], env=self.env,
                              capture_output=True, text=True, cwd=ROOT, timeout=60)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("fast_forward_main needs a branch to advance", done.stderr)

    def test_the_run_creates_a_missing_branch_and_logs_it(self):
        store = self.start_run(["--base", "main", "--branch", "new-landing"])
        self.assertEqual(wt.head(self.repo, "new-landing"), wt.head(self.repo, "main"))
        self.assertIn("created branch new-landing from base main", timeline_text(store))
        self.assertEqual(store.read("/bootstrap/base_branch"), "main")
        self.assertEqual(store.read("/bootstrap/branch"), "new-landing")

    def test_a_cascade_override_with_a_morph_model_refuses_the_whole_run(self):
        done = subprocess.run(self.base_argv() + ["--cascade",
                                                  '[{"provider": "openai-compatible", '
                                                  '"model": "anthropic-morph-kimik31"}]'],
                              env=self.env, capture_output=True, text=True, cwd=ROOT, timeout=60)
        self.assertNotEqual(done.returncode, 0, "a morph cascade model must exit non-zero")
        self.assertIn("morph_refused", done.stderr)

    def test_a_dag_named_repo_still_refuses_a_missing_base(self):
        """A DAG that sets "repo" is the effective repo: --base validates against it."""
        dag = self.tmp / "dag-repo.json"
        dag.write_text(json.dumps({"repo": str(self.repo), "tasks": [
            {"id": "a", "title": "A", "goal": "build a", "owns": ["src/a.txt"],
             "acceptance": "true"}]}))
        argv = [sys.executable, "-m", "jbi", "--dag", str(dag), "--offline", "--no-tailscale",
                "--no-menu", "--base", "ghost", "--start", "--registry", str(self.tmp / "reg.json")]
        done = subprocess.run(argv, env=self.env, capture_output=True, text=True,
                              cwd=ROOT, timeout=60)
        self.assertNotEqual(done.returncode, 0, "a missing base must exit non-zero")
        self.assertIn("base_missing", done.stderr)
        self.assertEqual(list(self.home.glob("runs/*/worktrees")), [],
                         "the refusal left task worktrees behind")

    def test_a_dry_run_refuses_a_missing_branch_at_the_cli(self):
        done = subprocess.run(self.base_argv() + ["--dry-run", "--base", "main",
                                                  "--branch", "dry-landing", "--start"],
                              env=self.env, capture_output=True, text=True, cwd=ROOT, timeout=60)
        self.assertNotEqual(done.returncode, 0, "a dry run must not create a landing branch")
        self.assertIn("branch_missing", done.stderr)
        self.assertIsNone(wt.head(self.repo, "dry-landing"),
                          "the dry run created the branch in the repository")

    def start_run(self, extra: list[str], offline: bool = True) -> Store:
        """Start one run, wait for its registry, stop it, and return its store."""
        argv = [sys.executable, "-m", "jbi", "--repo", str(self.repo), "--dag", str(self.dag),
                "--no-tailscale", "--no-menu", "--registry", str(self.tmp / "reg.json")]
        if offline:
            argv.append("--offline")
        registry = self.tmp / "reg.json"
        proc = subprocess.Popen(argv + extra, env=self.env, cwd=ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not registry.exists():
            time.sleep(0.1)
        self.assertTrue(registry.exists(), "the run did not come up")
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
        run_db = sorted(self.home.glob("runs/*/run.db"))[-1]
        store = Store(run_db)
        self.addCleanup(store.close)
        return store

    def test_a_json_cascade_enables_its_named_providers(self):
        """A JSON cascade seeds every provider it names, or select silently falls back."""
        store = self.start_run(["--cascade", '[{"provider": "claude", "model": "claude-opus-5-5"}]'],
                               offline=False)
        self.assertTrue(store.read("/accounts/claude/enabled"),
                        "the JSON cascade left its provider disabled")
        self.assertTrue(store.read("/accounts/agy/enabled"), "the default provider stayed disabled")
        self.assertFalse(store.read("/accounts/codex/enabled"),
                         "an unrelated provider got enabled")

    def test_the_founder_cascade_enables_claude_and_agy(self):
        store = self.start_run(["--cascade", "founder"], offline=False)
        self.assertTrue(store.read("/accounts/claude/enabled"))
        self.assertTrue(store.read("/accounts/agy/enabled"))


class BranchSplitRun(RunFixture):
    """--base X --branch Y: task branches cut from X, and only Y advances."""

    def test_tasks_cut_from_x_while_the_landing_branch_y_advances(self):
        sup = self.make([task("a", owns=["src/a.txt"]),
                         task("b", deps=["a"], owns=["src/b.txt"])], rigor="vibe")
        repo = self.repo
        main_before = wt.head(repo, "main")
        wt.git(repo, "branch", "cut-base")
        wt.git(repo, "checkout", "-q", "cut-base")
        (repo / "XFILE").write_text("x\n")
        wt.commit_all(repo, "advance the cut base")
        wt.git(repo, "checkout", "-q", "main")
        x_head = wt.head(repo, "cut-base")
        self.assertNotEqual(x_head, main_before, "the fixture must give X its own commit")
        wt.ensure_branch(repo, "landing", "cut-base")       # what the CLI does for a new --branch
        self.store.commit("flags", Changes().set("bootstrap", "base_branch", "cut-base")
                          .set("bootstrap", "branch", "landing"), {})
        sup.start_execution()
        self.assertTrue(self.hooks.merged_event.wait(90), self.store.read("/timeline"))
        self.assertEqual(self.status("a"), "merged")
        self.assertEqual(self.status("b"), "merged")
        # Every task branch cut from X: the lease records X's head as its base commit.
        self.assertEqual(sup.tracking.get_task("a")["base_commit"], x_head)
        self.assertEqual(sup.tracking.get_task("b")["base_commit"], x_head)
        # The landing branch advanced to the last verified squash; X and main stayed pinned.
        self.assertEqual(wt.head(repo, "landing"), self.store.read("/plan/tasks/b/commit"))
        self.assertIn("a attempt 1", wt.git(repo, "show", "landing:src/a.txt"))
        self.assertIn("b attempt 1", wt.git(repo, "show", "landing:src/b.txt"))
        self.assertEqual(wt.head(repo, "cut-base"), x_head, "the base branch moved")
        self.assertEqual(wt.head(repo, "main"), main_before, "main moved instead of the landing branch")


class CascadeOverride(unittest.TestCase):
    """The cascade is a run override: claude opus 5.5, then Neon, then Carbon. No morph."""

    def state(self, cascade=True, accounts=None) -> dict:
        state = {"config": {"profile": "balanced"},
                 "accounts": accounts if accounts is not None else {
                     "claude": {"enabled": True, "status": "ok"},
                     "agy": {"enabled": True, "status": "ok"}}}
        if cascade:
            state["config"]["provider_cascade"] = [dict(entry) for entry in models.FOUNDER_CASCADE]
        return state

    def test_the_catalog_holds_no_morph_id(self):
        text = CATALOG.read_text()
        self.assertNotIn("morph", text)
        self.assertNotIn("kimik", text)
        self.assertNotIn("dsv41", text)

    def test_the_founder_cascade_names_two_rungs(self):
        self.assertEqual([e["provider"] for e in models.FOUNDER_CASCADE],
                         ["claude", "agy", "agy"])
        self.assertEqual([e["model"] for e in models.FOUNDER_CASCADE],
                         ["claude-opus-5-5", "gemini-4-neon-high", "gemini-4-carbon-high"])

    def test_the_default_preference_order_is_unchanged(self):
        self.assertEqual(models.PREFERENCE, ("agy", "codex", "claude", "openai-compatible", "simulator"))
        choice = models.select(self.state(cascade=False), "implement")
        self.assertEqual(choice.provider, "agy", "the default order moved")

    def test_the_cascade_walks_in_order(self):
        choice = models.select(self.state(), "implement")
        self.assertEqual((choice.provider, choice.model), ("claude", "claude-opus-5-5"))
        accounts = {"claude": {"enabled": False, "status": "unknown"},
                    "agy": {"enabled": True, "status": "ok"}}
        choice = models.select(self.state(accounts=accounts), "implement")
        self.assertEqual((choice.provider, choice.model), ("agy", "gemini-4-neon-high"))

    def test_neon_falls_back_to_carbon_on_a_model_lane_cooldown(self):
        cooldowns = models.ProviderCooldowns()
        cooldowns.mark("claude", 60.0)                  # rung 1 is cooling
        cooldowns.mark("agy/gemini-4-neon-high", 60.0)  # and Neon's own lane is burned
        choice = models.select(self.state(), "implement", cooldowns=cooldowns)
        self.assertEqual((choice.provider, choice.model), ("agy", "gemini-4-carbon-high"))

    def test_a_cooling_provider_is_skipped_to_the_next_cascade_level(self):
        cooldowns = models.ProviderCooldowns()
        cooldowns.mark("claude", 60.0)
        choice = models.select(self.state(), "implement", cooldowns=cooldowns)
        self.assertEqual((choice.provider, choice.model), ("agy", "gemini-4-neon-high"))

    def test_a_task_preference_outranks_the_cascade(self):
        task = {"id": "x", "override": {"prefer": [{"provider": "agy", "model": "gemini-4-neon-high"}]}}
        choice = models.select(self.state(), "implement", task=task)
        self.assertEqual((choice.provider, choice.model), ("agy", "gemini-4-neon-high"))

    def test_a_pin_still_runs_on_its_model_alone(self):
        task = {"id": "x", "override": {"pin": {"provider": "claude", "model": "claude-opus-5-5"}}}
        choice = models.select(self.state(), "implement", task=task)
        self.assertEqual((choice.provider, choice.model), ("claude", "claude-opus-5-5"))


class CascadeFlag(unittest.TestCase):
    """--cascade founder installs the founder order; a morph entry refuses at start."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-cascade-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)
        self.store = make_store(self.tmp, self.repo)
        self.addCleanup(self.store.close)

    def test_founder_installs_the_founder_order(self):
        cli.install_cascade(self.store, "founder")
        self.assertEqual(self.store.read("/config/provider_cascade"),
                         [dict(entry) for entry in models.FOUNDER_CASCADE])
        self.assertIn("claude/claude-opus-5-5", timeline_text(self.store))

    def test_a_json_list_is_stored_as_the_override(self):
        cli.install_cascade(self.store, '[{"provider": "agy", "model": "gemini-4-neon-high"}]')
        self.assertEqual(self.store.read("/config/provider_cascade"),
                         [{"provider": "agy", "model": "gemini-4-neon-high"}])

    def test_a_morph_model_refuses_at_start(self):
        with self.assertRaises(SystemExit) as caught:
            cli.install_cascade(self.store,
                                '[{"provider": "openai-compatible", "model": "anthropic-morph-dsv41flash"}]')
        self.assertIn("morph_refused", str(caught.exception))
        self.assertIn("401", str(caught.exception))
        self.assertIsNone(self.store.read("/config/provider_cascade"),
                          "a refused cascade was stored anyway")

    def test_a_morph_refuses_in_any_case_and_on_the_provider(self):
        """The guard casefolds: Morph in a model or in a provider name refuses too."""
        for entry in ({"provider": "openai-compatible", "model": "Anthropic-Morph-dsv41"},
                      {"provider": "MORPH", "model": "dsv41"},
                      {"provider": "morph-labs", "model": "dsv41"}):
            with self.assertRaises(SystemExit) as caught:
                cli.install_cascade(self.store, json.dumps([entry]))
            self.assertIn("morph_refused", str(caught.exception), entry)
            self.assertIn("401", str(caught.exception), entry)
        self.assertIsNone(self.store.read("/config/provider_cascade"),
                          "a refused cascade was stored anyway")

    def test_a_non_morph_mixed_case_entry_still_installs(self):
        cli.install_cascade(self.store, '[{"provider": "Claude", "model": "Claude-Opus-5-5"}]')
        self.assertEqual(self.store.read("/config/provider_cascade"),
                         [{"provider": "Claude", "model": "Claude-Opus-5-5"}])

    def test_cascade_providers_names_every_distinct_provider(self):
        self.assertEqual(cli.cascade_providers("founder"), {"claude", "agy"})
        self.assertEqual(cli.cascade_providers(
            '[{"provider": "claude", "model": "a"}, {"provider": "claude", "model": "b"},'
            ' {"provider": "agy", "model": "c"}]'), {"claude", "agy"})
        self.assertEqual(cli.cascade_providers('[{"provider": "agy"}]'), {"agy"})
        self.assertEqual(cli.cascade_providers(None), set())
        self.assertEqual(cli.cascade_providers(""), set())
        self.assertEqual(cli.cascade_providers("{oops"), set(),
                         "invalid JSON names nothing; install_cascade refuses it")

    def test_bad_cascade_json_refuses_at_start(self):
        for bad in ("{oops", "[]", "not json", '[{"model": "m"}]'):
            with self.assertRaises(SystemExit) as caught:
                cli.install_cascade(self.store, bad)
            self.assertIn("cascade_invalid", str(caught.exception), bad)

    def test_an_entry_without_a_model_resolves_from_the_tier(self):
        cli.install_cascade(self.store, '[{"provider": "agy"}]')
        self.assertEqual(self.store.read("/config/provider_cascade"), [{"provider": "agy"}])


if __name__ == "__main__":
    unittest.main()
