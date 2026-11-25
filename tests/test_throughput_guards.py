"""Unit tests for throughput upgrades and rswarm-ported error-prevention guards."""

from __future__ import annotations

import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from jbi import briefs, dag_loader, guards, models, providers, rigor, shim, store, waves, worktrees as wt
from jbi.ci_adapter import CIAdapter
from jbi.llm import Prompt


class TestGuards(unittest.TestCase):
    def test_check_syntax_json_py_ts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good_json = root / "ok.json"
            good_json.write_text('{"a": 1}\n', encoding="utf-8")
            self.assertIsNone(guards.check_syntax(good_json))

            bad_json = root / "bad.json"
            bad_json.write_text('{"a": }\n', encoding="utf-8")
            self.assertIn("invalid JSON", guards.check_syntax(bad_json) or "")

            bad_py = root / "bad.py"
            bad_py.write_text("def broken(:\n    pass\n", encoding="utf-8")
            self.assertIn("Python syntax error", guards.check_syntax(bad_py) or "")

            good_ts = root / "ok.ts"
            good_ts.write_text('export const x = { a: "{" }; // }\n', encoding="utf-8")
            self.assertIsNone(guards.check_syntax(good_ts))

            bad_ts = root / "bad.ts"
            bad_ts.write_text("export function f() {\n  return [1, 2;\n}\n", encoding="utf-8")
            self.assertIn("mismatched", guards.check_syntax(bad_ts) or "")

    def test_guard_write_refuses_bad_json_and_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pkg = root / "package.json"
            pkg.write_text('{"name": "demo", "version": "1.0.0", "dependencies": {"a": "1.0.0"}}\n', encoding="utf-8")
            self.assertIn("invalid JSON", guards.guard_write(pkg, "{bad") or "")
            self.assertIn("destructive truncation", guards.guard_write(pkg, "{}") or "")
            self.assertIsNone(guards.guard_write(pkg, '{"name": "demo", "version": "1.0.1", "dependencies": {"a": "1.0.0"}}\n'))

    def test_safe_edit_hint_closest_and_multimatch(self) -> None:
        content = "line one\nconst alpha = 1;\nconst beta = 2;\nconst alpha = 1;\n"
        multi = guards.safe_edit_hint(content, "const alpha = 1;", "sample.ts")
        self.assertIn("matched 2 times", multi)
        self.assertIn("2, 4", multi)

        miss = guards.safe_edit_hint(content, "const betaa = 2;", "sample.ts")
        self.assertIn("Closest region", miss)
        self.assertIn("const beta = 2;", miss)


class TestWorktreeHygiene(unittest.TestCase):
    def test_purge_tsbuildinfo_and_prepare_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a").mkdir()
            (root / "a" / "tsconfig.tsbuildinfo").write_text("stale", encoding="utf-8")
            (root / "node_modules" / "pkg").mkdir(parents=True)
            (root / "node_modules" / "pkg" / "keep.tsbuildinfo").write_text("keep", encoding="utf-8")
            purged = wt.purge_tsbuildinfo(root)
            self.assertEqual(purged, ["a/tsconfig.tsbuildinfo"])
            self.assertTrue((root / "node_modules" / "pkg" / "keep.tsbuildinfo").exists())

            marker_file = root / "ran.txt"
            notes1 = wt.prepare_worktree(root, setup_commands=[f"echo hi >> {marker_file}"], cache_key="sha-1")
            self.assertTrue(any("ran setup" in n for n in notes1))
            notes2 = wt.prepare_worktree(root, setup_commands=[f"echo hi >> {marker_file}"], cache_key="sha-1")
            self.assertTrue(any("cache hit" in n for n in notes2))
            self.assertEqual(len(marker_file.read_text(encoding="utf-8").splitlines()), 1)

    def test_dotfile_owns_boundary_and_zero_terminated_dirt(self):
        for bad in [".env", ".github/workflows/ci.yml", "../env"]:
            self.assertFalse(wt.path_owned(bad, ["env", "github"]))
        self.assertTrue(wt.path_owned("./env", ["env"]))
        self.assertTrue(wt.path_owned(".env", [".env"]))
        self.assertFalse(wt.path_owned(".github", [".github/workflows"]))

    def test_rewritten_pointer_never_replaces_the_registered_worktree_metadata(self):
        tmp = Path(tempfile.mkdtemp(prefix="jbi-pin-restart-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        wt.init_repo(repo)
        (repo / "base").write_text("base")
        base = wt.commit_all(repo, "base")
        path = wt.create_worktree(repo, "task", base, tmp / "worktrees")
        before = wt.pin_git_metadata(path, repo)
        hostile = path / "fakegit"
        hostile.mkdir()
        (hostile / "HEAD").write_text("ref: refs/heads/evil")
        (hostile / "commondir").mkdir()
        (path / ".git").write_text("gitdir: fakegit")
        after = wt.pin_git_metadata(path, repo)
        self.assertEqual(before, after)
        self.assertNotEqual(after[0], hostile.resolve())
        wt.unpin_git_metadata(path)
        self.assertEqual(wt.pin_git_metadata(path, repo), before)
        self.assertEqual(wt.current_branch_or_none(path), "jbi/task")
        wt.pin_git_metadata(path, repo)
        self.assertEqual(wt.head(path), wt.head(repo, "jbi/task"))
        import jbi.resume as resume
        ok, why = resume._worktree_contents_committed(repo, path)
        self.assertFalse(ok, why)
        self.assertEqual(wt.git(path, "rev-parse", "HEAD"), base)
        self.assertEqual(wt.git(path, "status", "--porcelain"), "?? fakegit/")

    def test_host_admission_check(self) -> None:
        with mock.patch("os.getloadavg", return_value=(40.0, 30.0, 20.0)), mock.patch("os.cpu_count", return_value=8):
            reason = wt.host_admission_check(max_load_per_cpu=2.0)
            self.assertIsNotNone(reason)
            self.assertIn("host load", reason or "")
        with mock.patch("os.getloadavg", return_value=(4.0, 4.0, 4.0)), mock.patch("os.cpu_count", return_value=8):
            self.assertIsNone(wt.host_admission_check(max_load_per_cpu=2.0))


class TestWavesAndStrictSpec(unittest.TestCase):
    def test_prioritize_ready_and_owns_conflicts(self) -> None:
        plan = waves.compile_plan([
            {"id": "a", "title": "A", "goal": "Leaf task with no downstream dependents.", "deps": [], "owns": ["src/shared.ts"], "acceptance": "echo a"},
            {"id": "b", "title": "B", "goal": "Critical root task unblocking c and d.", "deps": [], "owns": ["src/shared.ts"], "acceptance": "echo b"},
            {"id": "c", "title": "C", "goal": "Downstream task c.", "deps": ["b"], "owns": ["src/c.ts"], "acceptance": "echo c"},
            {"id": "d", "title": "D", "goal": "Downstream task d.", "deps": ["b"], "owns": ["src/d.ts"], "acceptance": "echo d"},
        ])
        ordered = waves.prioritize_ready(plan)
        self.assertEqual(ordered[0], "b")

        # Finish-first boost puts a retried task ahead of fresh tasks.
        plan["tasks"]["a"]["attempts"] = 1
        self.assertEqual(waves.prioritize_ready(plan)[0], "a")

        conflicts = waves.detect_wave_owns_conflicts(plan)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual((conflicts[0]["task_a"], conflicts[0]["task_b"]), ("a", "b"))

    def test_ready_tasks_stay_with_their_node(self) -> None:
        plan = waves.compile_plan([
            {"id": "a1", "title": "A1", "goal": "One", "deps": [], "owns": ["src/a1.ts"], "acceptance": "true", "node": "alpha"},
            {"id": "b1", "title": "B1", "goal": "One", "deps": [], "owns": ["src/b1.ts"], "acceptance": "true", "node": "beta"},
            {"id": "a2", "title": "A2", "goal": "Two", "deps": [], "owns": ["src/a2.ts"], "acceptance": "true", "node": "alpha"},
            {"id": "b2", "title": "B2", "goal": "Two", "deps": [], "owns": ["src/b2.ts"], "acceptance": "true", "node": "beta"},
        ])
        self.assertEqual(waves.prioritize_ready(plan, ["a1", "b1", "a2", "b2"]), ["a1", "a2", "b1", "b2"])

    def test_a_missing_worktree_can_restore_the_exact_parked_ref(self) -> None:
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp(prefix="jbi-restore-missing-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        wt.init_repo(repo)
        (repo / "base").write_text("base")
        base = wt.commit_all(repo, "base")
        path = wt.create_worktree(repo, "task", base, tmp / "worktrees")
        (path / "new").write_text("preserved")
        saved = wt.commit_all(path, "saved")
        parked = "jbi-parked/task-20261123"
        wt.git(repo, "branch", "-m", "jbi/task", parked)
        wt.remove_worktree(repo, path)
        recovered = wt.restore_worktree(repo, "task", base, tmp / "worktrees", expected_head=saved)
        self.assertEqual(wt.head(recovered, "HEAD"), saved)
        self.assertEqual((recovered / "new").read_text(), "preserved")


    def test_wave_preflight_blocks_open_decision_and_missing_tool(self) -> None:
        plan = waves.compile_plan([
            {"id": "t1", "title": "T1", "goal": "Task one detailed goal.", "deps": [], "owns": ["src/t1.ts"], "acceptance": "echo 1"},
            {"id": "t2", "title": "T2", "goal": "Task two detailed goal.", "deps": ["t1"], "owns": ["src/t2.ts"], "acceptance": "echo 2"},
        ])
        res = waves.wave_preflight(
            plan, 2,
            open_requests=[{"id": "voice-6", "status": "open", "task": "t2", "title": "Missing inboxId"}],
            required_tools=["definitely-missing-binary-xyz"],
        )
        self.assertFalse(res["ok"])
        joined = "\n".join(res["blockers"])
        self.assertIn("unmerged upstream task t1", joined)
        self.assertIn("voice-6", joined)
        self.assertIn("definitely-missing-binary-xyz", joined)

    def test_strict_spec_and_conflict_refusal_in_dag_loader(self) -> None:
        raw = json.dumps({
            "project": "demo",
            "repo": ".",
            "nodes": [
                {"id": "n1", "title": "N1", "goal": "short", "deps": [], "owns": ["src/x.ts"], "acceptance": "true"},
            ],
        })
        with self.assertRaises(dag_loader.Invalid):
            dag_loader.load(raw, strict_spec=True)

        conflict_raw = json.dumps({
            "project": "demo",
            "repo": ".",
            "nodes": [
                {"id": "n1", "title": "N1", "goal": "Implement module one cleanly.", "deps": [], "owns": ["src/x.ts"],
                 "acceptance": "echo 1", "contracts": ["interface A {}"]},
                {"id": "n2", "title": "N2", "goal": "Implement module two cleanly.", "deps": [], "owns": ["src/x.ts"],
                 "acceptance": "echo 2", "contracts": ["interface B {}"]},
            ],
        })
        with self.assertRaises(dag_loader.Invalid):
            dag_loader.load(conflict_raw, strict_spec=True, forbid_wave_owns_conflicts=True)


class TestModelsPoolAnd429Retry(unittest.TestCase):
    def test_pool_rotation_and_cooldown_skip(self) -> None:
        now_t = [100.0]
        cd = models.ProviderCooldowns(clock=lambda: now_t[0])
        pool = models.ProviderPool([("agy", "m-agy"), ("agy", "m-agy"), ("claude", "m-claude")])
        picks = [pool.next(["agy", "claude"], "S", cooldowns=cd).provider for _ in range(3)]
        self.assertEqual(picks, ["agy", "agy", "claude"])

        cd.mark("agy", 30.0)
        self.assertEqual(pool.next(["agy", "claude"], "S", cooldowns=cd).provider, "claude")
        now_t[0] = 135.0
        self.assertFalse(cd.cooling("agy"))

    def test_completer_429_retry_and_cooldown_failover(self) -> None:
        state = {
            "config": {"profile": "balanced"},
            "accounts": {
                "agy": {"enabled": True, "status": "ok"},
                "claude": {"enabled": True, "status": "ok"},
            },
        }
        cd = models.ProviderCooldowns()
        calls: list[str] = []

        def fake_cli(provider: str, model: str, prompt: Prompt, timeout: int):
            calls.append(provider)
            if provider == "agy":
                raise providers.CallFailed(1, "HTTP 429 rate limit exceeded", "")
            return "ok-from-claude", {"tokens_in": 10, "tokens_out": 5}

        with mock.patch.object(providers, "_cli_call", side_effect=fake_cli):
            complete = providers.completer(lambda: state, cooldowns=cd, max_429_retries=1, retry_backoff_s=0.0, cooldown_s=30.0)
            res = complete(Prompt(role="implement", system="sys", user="usr"))
        self.assertEqual(res.provider, "claude")
        self.assertEqual(calls, ["agy", "agy", "claude"])
        self.assertTrue(cd.cooling("agy"))


class TestRigorAndBriefs(unittest.TestCase):
    def test_wave_epic_and_effort_scaling(self) -> None:
        self.assertIn("wave-epic", rigor.PIPELINES)
        self.assertEqual([s.name for s in rigor.pipeline("wave-epic")], ["implement", "acceptance", "merge"])
        self.assertEqual(rigor.effort_for("implement", attempts=0), "medium")
        self.assertEqual(rigor.effort_for("implement", attempts=2), "high")
        self.assertLess(rigor.task_timeouts({"effort": "XS"})["max_turns"],
                        rigor.task_timeouts({"effort": "XL"})["max_turns"])

    def test_discover_shared_helpers_and_brief_render(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "shared").mkdir()
            (root / "shared" / "scope.ts").write_text("export const x = 1;\n", encoding="utf-8")
            helpers = briefs.discover_shared_helpers(root)
            self.assertEqual(helpers, ["shared/scope.ts"])
            text = briefs.render(
                task={"id": "t1", "title": "T1", "goal": "Goal", "owns": ["src/a.ts"], "acceptance": "echo ok"},
                role="implement", stage="implement", attempt=0, receipt=root / "r.txt",
                shared_helpers=helpers, checkpoint_note="Resuming after timeout",
            )
            self.assertIn("shared/scope.ts", text)
            self.assertIn("Resuming after timeout", text)
            self.assertLessEqual(len(text.splitlines()), briefs.MAX_LINES)


class TestShimWatchdogAndEpicLane(unittest.TestCase):
    def test_shim_idle_timeout_kills_hung_process(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            spec = {
                "cmd": [sys.executable, "-c", "import time; time.sleep(30)"],
                "cwd": str(root),
                "provider": "simulator",
                "env": {},
                "parent_pid": os.getpid(),
                "status": str(root / "status.json"),
                "log": str(root / "out.log"),
                "grace": 0.2,
                "idle_timeout_sec": 0.4,
            }
            code = shim.Shim(spec).run()
            self.assertNotEqual(code, 0)
            st = json.loads((root / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(st["reason"], "idle-timeout")

    def test_ready_handover_targets_the_epic_branch_through_the_fake_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            wt.init_repo(repo)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            base_sha = wt.commit_all(repo, "init")
            assert base_sha
            wt.git(repo, "branch", "epic/wave-4", base_sha)
            task_wt = wt.create_worktree(repo, "t1", "epic/wave-4", root / "wts")
            (task_wt / "feature.ts").write_text("export const v = 1;\n", encoding="utf-8")
            head = wt.commit_all(task_wt, "add feature")
            calls: list[list[str]] = []

            def runner(argv, cwd):
                calls.append(list(argv))
                out = ""
                if argv[0] == "ci-conductor":
                    out = json.dumps({"type": "queued", "job": "pr-9", "expect": "notice-on-completion",
                                      "text": f"#9 at {head[:9]} is queued."})
                elif argv[:3] == ["gh", "pr", "list"]:
                    out = "[]"
                elif argv[:3] == ["gh", "pr", "create"]:
                    out = "https://example.test/pull/9"
                return subprocess.CompletedProcess(argv, 0, out, "")

            result = CIAdapter(runner=runner).handover(
                repo, {"id": "t1", "title": "T", "goal": "G"}, base="epic/wave-4", verb="ready")
            self.assertEqual(result.pr_number, 9)
            self.assertIn(["ci-conductor", "ready", "9", "--head", head], calls)
            create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
            self.assertEqual(create[create.index("--base") + 1], "epic/wave-4")
            self.assertEqual(result.base_commit, base_sha)
            self.assertEqual(wt.head(repo, "main"), base_sha, "the adapter never moves a branch")
            self.assertEqual(wt.head(repo, "epic/wave-4"), base_sha)

    def test_link_ignored_deps_keeps_worktree_clean_and_reclaim_safe(self) -> None:
        from jbi import resume
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            wt.init_repo(repo)
            (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            base_sha = wt.commit_all(repo, "init")
            assert base_sha
            (repo / "node_modules" / "pkg").mkdir(parents=True)
            (repo / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
            task_wt = wt.create_worktree(repo, "t-deps", "main", root / "wts")
            linked = wt.link_ignored_deps(repo, task_wt)
            self.assertEqual(linked, ["node_modules"])
            self.assertTrue((task_wt / "node_modules").is_dir())
            self.assertTrue((task_wt / "node_modules" / "pkg").is_symlink())
            self.assertEqual(wt.dirty_paths(task_wt), [])
            ok, why = resume._worktree_contents_committed(repo, task_wt)
            self.assertTrue(ok, why)

    def test_agy_stream_json_result_envelope_parses_structured_output_and_usage(self) -> None:
        from claude_mux_lite import credentials
        from unittest import mock
        bridge = credentials._AgyBridge([])
        self.addCleanup(bridge.close)
        ndjson = "\n".join([
            json.dumps({"event": "init", "conversation_id": "conv-123"}),
            json.dumps({"event": "result", "result": {
                "conversation_id": "conv-123",
                "status": "SUCCESS",
                "structured_output": {"text": "DONE", "tool_calls": []},
                "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_tokens": 80},
            }}),
        ])
        with mock.patch.object(bridge, "_invoke", return_value=subprocess.CompletedProcess([], 0, ndjson, "")):
            code, body, _ = bridge.handle(json.dumps({"model": "gemini-4-neon-medium", "messages": []}).encode(), 10.0)
        self.assertEqual(code, 200)
        ans = json.loads(body.decode())
        self.assertEqual(ans["content"][0]["text"], "DONE")
        self.assertEqual(ans["usage"]["input_tokens"], 120)
        self.assertEqual(ans["usage"]["cache_read_input_tokens"], 80)


if __name__ == "__main__":
    unittest.main()
