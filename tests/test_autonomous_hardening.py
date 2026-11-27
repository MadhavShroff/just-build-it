"""Unit tests for the 5 autonomous-execution hardening fixes in `jbi`."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jbi import ci_adapter, cli, protocol, providers, rigor, supervisor, waves
from jbi import worktrees as wt
from jbi.ci_adapter import CIAdapter, CIAdapterError
from jbi.store import Changes, Store
from jbi.supervisor import Supervisor
from jbi.tracking import LeaseError, Tracking


class _NoopHooks:
    def bootstrap_finished(self, *args):
        pass

    task_merged = wave_completed = all_merged = stopped = bootstrap_finished


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c", "protocol.file.allow=always",
            "-c", "core.hooksPath=/dev/null",
            "-c", "user.name=jbi-test",
            "-c", "user.email=jbi@localhost",
            "-C", str(cwd),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _make_repo_with_origin(tmp: Path) -> tuple[Path, Path, str]:
    origin = tmp / "origin.git"
    _git(tmp, "init", "-q", "--bare", str(origin))
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    wt.init_repo(repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    base = wt.commit_all(repo, "base")
    assert base is not None
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    return origin, repo, base


def _make_supervisor(tmp: Path, repo: Path, tasks: list[dict], *,
                     rigor_level: str = "high", name: str = "run") -> tuple[Supervisor, Store]:
    run_dir = tmp / name
    run_dir.mkdir(parents=True, exist_ok=True)
    state = protocol.initial_state(
        f"r-{name}", "now", name="demo", mode="existing",
        target=str(repo), max_sessions=2, lanes=1, tmux="",
    )
    state["config"]["rigor"] = rigor_level
    state["plan"] = waves.compile_plan(tasks, 1)
    store = Store(run_dir / "run.db", state)
    sup = Supervisor(store, _NoopHooks(), run_dir, repo, use_tmux=False)
    sup.ci_poll_interval = 0.02
    sup.sibling_lease_poll_interval = 0.02
    return sup, store


class StageStallCounterTests(unittest.TestCase):
    """Fix 1: consecutive non-advancing stage stall counter & 25-attempt circuit breaker."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-stall-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        _origin, self.repo, self.base = _make_repo_with_origin(self.tmp)

    def test_constants_and_stage_index(self) -> None:
        self.assertEqual(rigor.DECISION_AFTER, 5)
        self.assertEqual(rigor.MAX_CUMULATIVE_ATTEMPTS, 25)
        self.assertLess(rigor.stage_index("high", "implement"), rigor.stage_index("high", "verify"))
        self.assertLess(rigor.stage_index("high", "verify"), rigor.stage_index("high", "review-cascade"))
        self.assertLess(rigor.stage_index("high", "review-cascade"), rigor.stage_index("high", "merge"))
        self.assertEqual(rigor.stage_index("high", "nonexistent"), -1)

    def test_forward_progress_across_stages_resets_stall_and_completes_past_5_attempts(self) -> None:
        sup, store = _make_supervisor(
            self.tmp,
            self.repo,
            [{"id": "T1", "title": "T1", "goal": "G", "deps": [], "owns": ["src/t1.ts"], "acceptance": "true"}],
            rigor_level="high",
            name="forward",
        )
        self.addCleanup(store.close)
        self.addCleanup(sup.tracking.close)

        # Fail twice at each advancing stage (10 total failures > DECISION_AFTER=5), then succeed.
        fail_plan = [
            "implement", "implement",
            "acceptance", "acceptance",
            "verify", "verify",
            "review-cascade", "review-cascade",
            "merge", "merge",
        ]
        attempt_idx = {"n": 0}

        def fake_run_stage(task, stage, level, worktree, findings):
            cur = attempt_idx["n"]
            if cur < len(fail_plan) and stage.name == fail_plan[cur]:
                attempt_idx["n"] += 1
                if stage.role == "merge":
                    return False, "CI red on merge", None
                return False, f"failed at {stage.name}", "sim-s"
            if stage.role == "merge":
                store.commit("task.merged", Changes().set("plan", "tasks", "T1", "status", "merged"), {"task": "T1"})
                return True, "merged", None
            return True, "ok", "sim-s"

        sup._run_stage = fake_run_stage
        sup._run_task_body("T1")

        task_doc = store.read("/plan/tasks/T1")
        self.assertEqual(task_doc["status"], "merged")
        self.assertEqual(task_doc["attempts"], 10)
        self.assertLessEqual(task_doc.get("stage_stall_count", 0), 2)
        self.assertEqual(store.read("/requests") or {}, {})

    def test_five_consecutive_non_advancing_failures_open_request(self) -> None:
        sup, store = _make_supervisor(
            self.tmp,
            self.repo,
            [{"id": "T1", "title": "T1", "goal": "G", "deps": [], "owns": ["src/t1.ts"], "acceptance": "true"}],
            rigor_level="high",
            name="stall5",
        )
        self.addCleanup(store.close)
        self.addCleanup(sup.tracking.close)

        # Fail at verify, then regress to acceptance, then implement x3 => 5 non-advancing failures.
        fail_stages = ["verify", "acceptance", "implement", "implement", "implement"]
        idx = {"n": 0}

        def fake_run_stage(task, stage, level, worktree, findings):
            target = fail_stages[idx["n"]]
            if stage.name == target:
                idx["n"] += 1
                return False, f"boom at {stage.name}", "sim-s"
            return True, "ok", "sim-s"

        sup._run_stage = fake_run_stage
        sup._run_task_body("T1")

        task_doc = store.read("/plan/tasks/T1")
        self.assertEqual(task_doc["status"], "request")
        self.assertEqual(task_doc["attempts"], 5)
        self.assertEqual(task_doc["stage_stall_count"], 5)
        self.assertIsNotNone(store.read("/requests/r-T1-5"))
        self.assertIsNone(store.read("/requests/r-T1-4"))

    def test_hard_circuit_breaker_at_25_cumulative_attempts(self) -> None:
        sup, store = _make_supervisor(
            self.tmp,
            self.repo,
            [{"id": "T1", "title": "T1", "goal": "G", "deps": [], "owns": ["src/t1.ts"], "acceptance": "true"}],
            rigor_level="high",
            name="circuit25",
        )
        self.addCleanup(store.close)
        self.addCleanup(sup.tracking.close)

        # Oscillate implement (0) -> verify (2) so stall count keeps resetting to 1 or 2 (< 5),
        # proving the 25-attempt cumulative circuit breaker still halts runaway tasks.
        count = {"n": 0}

        def fake_run_stage(task, stage, level, worktree, findings):
            want = "implement" if (count["n"] % 2 == 0) else "verify"
            if stage.name == want:
                count["n"] += 1
                return False, f"oscillating failure {count['n']}", "sim-s"
            return True, "ok", "sim-s"

        sup._run_stage = fake_run_stage
        sup._run_task_body("T1")

        task_doc = store.read("/plan/tasks/T1")
        self.assertEqual(task_doc["status"], "request")
        self.assertEqual(task_doc["attempts"], 25)
        self.assertLess(task_doc["stage_stall_count"], rigor.DECISION_AFTER)
        self.assertIsNotNone(store.read("/requests/r-T1-25"))


class RichFailureExtractionTests(unittest.TestCase):
    """Fix 2: _summarize_findings and <evidence_dir>/run.log extraction."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-rich-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_summarize_findings_keeps_head_middle_markers_with_context_and_tail(self) -> None:
        lines = [f"noise line {i}" for i in range(240)]
        lines[50] = "FAIL tangram-backend/nest-backend/src/auth/auth.spec.ts"
        lines[90] = "  ● AuthService › rejects expired token"
        lines[91] = "    Expected: 401"
        lines[92] = "    Received: 200"
        lines[130] = "src/service.ts(14,3): error TS2345: Argument of type 'string' is not assignable."
        lines[170] = "e01-census: pinned store drifted (2 unreviewed entries)"

        out = supervisor._summarize_findings("\n".join(lines), max_lines=100)
        self.assertLessEqual(len(out), 100)
        # First 15 and last 15 lines preserved
        self.assertEqual(out[:15], lines[:15])
        self.assertEqual(out[-15:], lines[-15:])
        # Middle markers AND 2 lines of surrounding context preserved in order
        for target_idx in (48, 49, 50, 51, 52, 88, 90, 91, 92, 94, 128, 130, 132, 168, 170, 172):
            self.assertIn(lines[target_idx], out)
        # Deduplicated and strictly ordered
        indices = [lines.index(item) for item in out]
        self.assertEqual(indices, sorted(set(indices)))

    def test_format_failure_findings_and_confirm_merge_read_evidence_run_log(self) -> None:
        ev_dir = self.tmp / "ci-evidence-42"
        ev_dir.mkdir(parents=True)
        log_lines = [f"[info] step {i}" for i in range(200)]
        log_lines[40] = "FAIL tangram-backend/nest-backend/src/projects/project.spec.ts"
        log_lines[41] = "  ● ProjectService › enforces tenant boundary"
        log_lines[42] = "    Error: Expected ForbiddenException"
        log_lines[120] = "e01-census: 1 unreviewed store detected"
        (ev_dir / "run.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        findings = ci_adapter.format_failure_findings({
            "verdict": "red",
            "job_id": "20261127-l1",
            "pr_number": 42,
            "head_sha": "a" * 40,
            "failed_gates": ["backend-unit", "census"],
            "evidence_url": str(ev_dir),
            "first_failure_excerpt": "gate backend-unit exited 1",
        })
        joined = "\n".join(findings)
        self.assertIn("FAIL tangram-backend/nest-backend/src/projects/project.spec.ts", joined)
        self.assertIn("● ProjectService › enforces tenant boundary", joined)
        self.assertIn("Error: Expected ForbiddenException", joined)
        self.assertIn("e01-census: 1 unreviewed store detected", joined)

        # Also test confirm_merge on a polled RED job naming `evidence <dir>`
        _origin, repo, base = _make_repo_with_origin(self.tmp)
        head = "b" * 40
        job_text = (
            f"prs: #42@{head[:9]}\n"
            "started 1  finished 2\n"
            "verdict RED  failed gates: backend-unit\n"
            f"evidence {ev_dir}\n"
        )

        def runner(argv, cwd, env=None):
            if argv[0] == "ci-conductor" and argv[1] == "job":
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"type": "data", "job": "job-42", "text": job_text}), ""
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

        adapter = CIAdapter(runner=runner)
        handover = ci_adapter.HandoverResult(
            task="T1",
            branch="jbi/T1",
            pr_number=42,
            head_sha=head,
            reply={"type": "queued", "job": "job-42", "bound": {"task": "T1", "pr_number": 42, "head_sha": head}},
            base_commit=base,
        )
        with self.assertRaises(CIAdapterError) as caught:
            adapter.confirm_merge(repo, handover)
        err_msg = str(caught.exception)
        self.assertIn("no green verdict", err_msg)
        self.assertIn("FAIL tangram-backend/nest-backend/src/projects/project.spec.ts", err_msg)
        self.assertIn("e01-census: 1 unreviewed store detected", err_msg)

    def test_extract_evidence_failures_skips_nest_console_and_p02_sentinel_noise(self) -> None:
        ev_dir = self.tmp / "ci-evidence-gb"
        ev_dir.mkdir(parents=True)
        noisy_lines = [
            "\x1b[31m[Nest] 123 - ERROR [SnsSignature] SNS signing certificate refused\x1b[39m"
            for _ in range(150)
        ] + [
            "FAIL ../../../../../tmp/p02-census-results-abc/failed.body.ts",
            "  ● Console",
            "PASS unit src/ok.spec.ts",
            "FAIL integration src/platform-direct/native/__tests__/native-journeys.http.integration.spec.ts (14.8 s)",
            "  ● Native HTTP journeys › 1. creates a typed Table",
            "    expect(received).toMatchObject(expected)",
            "    - \"userId\": \"journey-owner\"",
            "      at Object.toMatchObject (native-journeys.http.integration.spec.ts:638:30)",
        ]
        (ev_dir / "run.log").write_text("\n".join(noisy_lines) + "\n", encoding="utf-8")
        got = ci_adapter.extract_evidence_failures(str(ev_dir), max_lines=80)
        joined = "\n".join(got)
        self.assertNotIn("[Nest]", joined)
        self.assertNotIn("p02-census-results", joined)
        self.assertIn("FAIL integration src/platform-direct/native/__tests__/native-journeys.http.integration.spec.ts", joined)
        self.assertIn("- \"userId\": \"journey-owner\"", joined)


class OriginBaseAutoMergeTests(unittest.TestCase):
    """Fix 3: _sync_with_origin_base before retry and before handover in _merge."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-sync-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin, self.repo, self.base = _make_repo_with_origin(self.tmp)

    def _advance_origin(self, rel_path: str, content: str, branch: str = "main") -> str:
        clone = self.tmp / f"pusher-{int(time.monotonic() * 1000000)}"
        _git(self.tmp, "clone", "-q", str(self.origin), str(clone))
        _git(clone, "checkout", "-q", "-B", branch, f"origin/{branch}")
        target = clone / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(clone, "add", rel_path)
        _git(clone, "commit", "-qm", f"advance {rel_path}")
        _git(clone, "push", "-q", "origin", f"{branch}:{branch}")
        sha = wt.head(clone, "HEAD")
        assert sha is not None
        return sha

    def test_clean_sync_merges_origin_base_and_updates_commit_records(self) -> None:
        sup, store = _make_supervisor(
            self.tmp,
            self.repo,
            [{"id": "T1", "title": "T1", "goal": "G", "deps": [], "owns": ["src/t1.ts"], "acceptance": "true"}],
            rigor_level="vibe",
            name="sync-clean",
        )
        self.addCleanup(store.close)
        self.addCleanup(sup.tracking.close)

        worktree = wt.create_worktree(self.repo, "T1", self.base, sup.run_dir / "worktrees")
        wt.pin_git_metadata(worktree, self.repo)
        (worktree / "src").mkdir(parents=True, exist_ok=True)
        (worktree / "src" / "t1.ts").write_text("export const t1 = 1;\n", encoding="utf-8")
        first_head = wt.commit_all(worktree, "T1 initial")
        assert first_head is not None
        sup._task_lease("T1", worktree)
        sup._task_update("T1", commit=first_head)
        sup.tracking.record_commit("T1", first_head)

        origin_tip = self._advance_origin("src/upstream.ts", "export const up = true;\n")
        ok, findings = sup._sync_with_origin_base("T1", worktree, "main")
        self.assertTrue(ok)
        self.assertEqual(findings, [])
        merged_head = wt.head(worktree, "HEAD")
        assert merged_head is not None
        self.assertNotEqual(merged_head, first_head)
        self.assertTrue(wt.is_ancestor(worktree, origin_tip, merged_head))
        self.assertEqual(store.read("/plan/tasks/T1/commit"), merged_head)
        self.assertEqual(sup.tracking.get_task("T1")["head_commit"], merged_head)

    def test_conflict_sync_commits_conflict_markers_on_host_and_widens_owns(self) -> None:
        # Commit an initial shared file plus an unowned file so origin advances both.
        (self.repo / "src").mkdir(parents=True, exist_ok=True)
        (self.repo / "src" / "shared.spec.ts").write_text("const v = 0;\n", encoding="utf-8")
        (self.repo / "src" / "other.ts").write_text("export const o = 0;\n", encoding="utf-8")
        base2 = wt.commit_all(self.repo, "add shared.spec.ts and other.ts")
        assert base2 is not None
        _git(self.repo, "push", "-q", "origin", "main")

        sup, store = _make_supervisor(
            self.tmp,
            self.repo,
            [{"id": "T1", "title": "T1", "goal": "G", "deps": [], "owns": ["src/t1.ts"], "acceptance": "true"}],
            rigor_level="vibe",
            name="sync-conflict",
        )
        self.addCleanup(store.close)
        self.addCleanup(sup.tracking.close)

        worktree = wt.create_worktree(self.repo, "T1", base2, sup.run_dir / "worktrees")
        wt.pin_git_metadata(worktree, self.repo)
        (worktree / "src" / "shared.spec.ts").write_text("const v = 'from-task';\n", encoding="utf-8")
        task_head = wt.commit_all(worktree, "T1 edit")
        assert task_head is not None
        sup._task_lease("T1", worktree)
        sup._task_update("T1", commit=task_head)
        sup.tracking.record_commit("T1", task_head)

        self._advance_origin("src/other.ts", "export const o = 99;\n")
        origin_tip = self._advance_origin("src/shared.spec.ts", "const v = 'from-origin';\n")
        ok, findings = sup._sync_with_origin_base("T1", worktree, "main")
        self.assertFalse(ok)
        self.assertEqual(len(findings), 1)
        self.assertIn("Host merged origin/main", findings[0])
        self.assertIn("Do NOT run git fetch or git merge", findings[0])
        self.assertIn("src/shared.spec.ts", findings[0])
        # Host merge committed cleanly so bwrap needs neither git fetch nor writes outside owns.
        merged_head = wt.head(worktree, "HEAD")
        assert merged_head is not None
        self.assertNotEqual(merged_head, task_head)
        self.assertTrue(wt.is_ancestor(worktree, origin_tip, merged_head))
        self.assertEqual(wt.dirty_paths(worktree), [])
        # Unowned upstream file landed cleanly on host; conflicted file holds both markers and is widened.
        self.assertEqual((worktree / "src" / "other.ts").read_text(encoding="utf-8"), "export const o = 99;\n")
        conflict_text = (worktree / "src" / "shared.spec.ts").read_text(encoding="utf-8")
        self.assertIn("<<<<<<<", conflict_text)
        self.assertIn("from-task", conflict_text)
        self.assertIn("from-origin", conflict_text)
        self.assertIn("src/shared.spec.ts", store.read("/plan/tasks/T1/owns"))
        live_lease = [l for l in sup.tracking.fleet()["leases"] if l["task_id"] == "T1" and l["released_at"] is None][0]
        self.assertIn("src/shared.spec.ts", live_lease["owns"])

    def test_merge_reruns_acceptance_after_clean_origin_sync_before_handover(self) -> None:
        sup, store = _make_supervisor(
            self.tmp,
            self.repo,
            [{
                "id": "T1", "title": "T1", "goal": "G", "deps": [],
                "owns": ["src/t1.ts"], "acceptance": "test -f src/upstream.ts && test -f src/t1.ts",
            }],
            rigor_level="fast",
            name="merge-acc",
        )
        self.addCleanup(store.close)
        self.addCleanup(sup.tracking.close)

        worktree = wt.create_worktree(self.repo, "T1", self.base, sup.run_dir / "worktrees")
        wt.pin_git_metadata(worktree, self.repo)
        (worktree / "src").mkdir(parents=True, exist_ok=True)
        (worktree / "src" / "t1.ts").write_text("export const x = 1;\n", encoding="utf-8")
        head1 = wt.commit_all(worktree, "T1 work")
        assert head1 is not None
        sup._task_lease("T1", worktree)
        sup._task_update("T1", commit=head1)
        sup.tracking.record_commit("T1", head1)

        self._advance_origin("src/upstream.ts", "export const u = 2;\n")
        acc_calls: list[str] = []
        orig_acc = sup._run_acceptance_command

        def spy_acc(task_doc, wt_path):
            acc_calls.append(wt.head(wt_path, "HEAD") or "")
            return orig_acc(task_doc, wt_path)

        handed_heads: list[str] = []

        def fake_handover(repo, task_doc, *, base="main", verb="ready", tracking=None):
            h = wt.head(worktree, "HEAD")
            assert h is not None
            handed_heads.append(h)
            sup._task_update("T1", status="merged", commit=h)
            sup._merge_ok["T1"] = True
            sup._merge_done["T1"].set()
            return ci_adapter.HandoverResult(
                task="T1", branch="jbi/T1", pr_number=77, head_sha=h,
                reply={"type": "queued", "job": "pr-77", "already_merged": True},
                base_commit=self.base,
            )

        with mock.patch.object(sup, "_run_acceptance_command", side_effect=spy_acc), \
                mock.patch.object(sup.ci_adapter, "handover", side_effect=fake_handover), \
                mock.patch.object(sup.ci_adapter, "pending", return_value=False), \
                mock.patch.object(sup, "_finish_verified_merge", return_value=True), \
                mock.patch.object(sup.ci_adapter, "confirm_merge", return_value={"squash_sha": "c" * 40}):
            ok = sup._merge("T1", "fast", worktree)

        self.assertTrue(ok)
        self.assertEqual(len(acc_calls), 1)
        self.assertEqual(len(handed_heads), 1)
        self.assertNotEqual(handed_heads[0], head1)
        self.assertEqual(acc_calls[0], handed_heads[0])


class LeaseAutoWidenAndSiblingWaitTests(unittest.TestCase):
    """Fix 4: Tracking.widen_lease, safe auto-widening, protected-path denial, and sibling lease wait."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-widen-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        _origin, self.repo, self.base = _make_repo_with_origin(self.tmp)

    def test_tracking_widen_lease_updates_task_and_lease_and_checks_conflicts(self) -> None:
        tr = Tracking(self.tmp / "fleet.db")
        self.addCleanup(tr.close)
        wt1 = wt.create_worktree(self.repo, "T1", "main", self.tmp / "wts")
        wt2 = wt.create_worktree(self.repo, "T2", "main", self.tmp / "wts")
        tr.create_task(title="T1", owns=["src/a.ts"], branch="jbi/T1", worktree=wt1, base_commit=self.base, task_id="T1")
        tr.create_task(title="T2", owns=["src/b.ts"], branch="jbi/T2", worktree=wt2, base_commit=self.base, task_id="T2")
        tr.dispatch("T1", session_id="s1", owns=["src/a.ts"], branch="jbi/T1", worktree=wt1)
        tr.dispatch("T2", session_id="s2", owns=["src/b.ts"], branch="jbi/T2", worktree=wt2)

        widened = tr.widen_lease("T1", ["src/a.spec.ts"])
        self.assertEqual(widened["owns"], ["src/a.spec.ts", "src/a.ts"])
        self.assertEqual(tr.get_task("T1")["owns"], ["src/a.spec.ts", "src/a.ts"])
        self.assertEqual(tr.authorize_write("T1", "src/a.spec.ts"), "src/a.spec.ts")

        with self.assertRaises(LeaseError) as caught:
            tr.widen_lease("T1", ["src/b.ts"])
        self.assertIn("T2", str(caught.exception))

    def test_safe_widenable_and_protected_infra_filters(self) -> None:
        files = [
            "src/feature/__tests__/helper.ts",
            "src/feature/service.spec.ts",
            "src/feature/util.test.ts",
            "test/auth.e2e-spec.ts",
            "tangram-backend/nest-backend/scripts/e01-census.mjs",
            "tangram-backend/nest-backend/src/projects/storage/index.ts",
            "tangram-backend/nest-backend/src/mikro-orm.config.ts",
            "tangram-backend/nest-backend/docs/runbooks/a02-auth-deployment.rc.json",
            "src/feature/prod-impl.ts",
            "scripts/ci",
            "scripts/ci-runner/worker.sh",
            "scripts/hooks/pre-push",
            ".claude/settings.json",
            "tangram-backend/.env.test",
        ]
        for rel in files:
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("// stub\n", encoding="utf-8")

        detail = "Blocked by lease scope or FAIL on:\n" + "\n".join(f"  FAIL {p}" for p in files)
        extracted = supervisor._extract_widenable_paths(detail, self.repo, ["src/owned.ts"], repo=self.repo)
        self.assertEqual(
            set(extracted),
            {
                "src/feature/__tests__/helper.ts",
                "src/feature/service.spec.ts",
                "src/feature/util.test.ts",
                "test/auth.e2e-spec.ts",
                "tangram-backend/nest-backend/scripts/e01-census.mjs",
                "tangram-backend/nest-backend/src/projects/storage/index.ts",
                "tangram-backend/nest-backend/src/mikro-orm.config.ts",
                "tangram-backend/nest-backend/docs/runbooks/a02-auth-deployment.rc.json",
            },
        )
        for forbidden in (
            "src/feature/prod-impl.ts",
            "scripts/ci",
            "scripts/ci-runner/worker.sh",
            "scripts/hooks/pre-push",
            ".claude/settings.json",
            "tangram-backend/.env.test",
        ):
            self.assertNotIn(forbidden, extracted)

        # Explicit LEASE GAP / [BLOCKED][LEASE] lines widen non-protected existing callers + *.module.ts,
        # while still blocking protected infra and package.json/yarn.lock.
        for extra_rel in ("src/auth/auth.module.ts", "tangram-ui/app/(app)/layout.tsx", "package.json"):
            p = self.repo / extra_rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("// stub\n", encoding="utf-8")
        gap_detail = (
            "FAIL src/auth/auth.module.ts\n"
            "- [BLOCKED][LEASE] lease excludes src/feature/prod-impl.ts and (tangram-ui/app/(app)/layout.tsx) and package.json."
        )
        gap_extracted = supervisor._extract_widenable_paths(gap_detail, self.repo, ["src/owned.ts"], repo=self.repo)
        self.assertEqual(
            set(gap_extracted),
            {"src/auth/auth.module.ts", "src/feature/prod-impl.ts", "tangram-ui/app/(app)/layout.tsx"},
        )
        self.assertNotIn("package.json", gap_extracted)

        # Non-existent new *.p02-rows.ts / MISSING_LEASE files inside existing directories widen cleanly,
        # while hallucinated parent directories and protected package.json stay blocked.
        (self.repo / "src/native-chart").mkdir(parents=True, exist_ok=True)
        new_file_detail = (
            "MISSING_LEASE: src/native-chart/native-chart.p02-rows.ts and src/native-chart/__tests__/new-rows.spec.ts\n"
            "MISSING_LEASE: nonexistent-folder/fake.p02-rows.ts and src/native-chart/package.json\n"
            "Verified: launch-d07.b2.spec.ts and native-chart.module.ts were checked in prose.\n"
        )
        new_extracted = supervisor._extract_widenable_paths(new_file_detail, self.repo, ["src/owned.ts"], repo=self.repo)
        self.assertEqual(
            set(new_extracted),
            {"src/native-chart/native-chart.p02-rows.ts", "src/native-chart/__tests__/new-rows.spec.ts"},
        )

    def test_initial_lease_conflict_withheld_by_admit_and_waits_without_failing(self) -> None:
        shared_dir = "src/shared/object-store/__tests__"
        shared_file = f"{shared_dir}/writer-classification.audit.spec.ts"
        target = self.repo / shared_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export {};\n", encoding="utf-8")
        base2 = wt.commit_all(self.repo, "add shared spec")
        assert base2 is not None
        _git(self.repo, "push", "-q", "origin", "main")

        sup, store = _make_supervisor(
            self.tmp,
            self.repo,
            [
                {"id": "T1", "title": "T1", "goal": "G1", "deps": [], "owns": [shared_file], "acceptance": "true"},
                {"id": "T2", "title": "T2", "goal": "G2", "deps": [], "owns": [f"{shared_dir}/**"], "acceptance": "true"},
            ],
            rigor_level="vibe",
            name="initial-lease-wait",
        )
        self.addCleanup(store.close)
        self.addCleanup(sup.tracking.close)
        sup.sibling_lease_poll_interval = 0.05

        wt1 = wt.create_worktree(self.repo, "T1", base2, sup.run_dir / "worktrees")
        wt.pin_git_metadata(wt1, self.repo)
        sup._task_lease("T1", wt1)
        sup.tracking.record_commit("T1", base2)
        # Even when T1 leaves _running (e.g., status == "request"), its unreleased lease blocks _admit for T2.
        sup._task_update("T1", status="request")
        sup._admit()
        self.assertNotIn("T2", sup._running)

        # And if _run_task_body("T2") runs directly while T1 holds the overlapping lease, T2 waits instead of failing.
        def fake_stage(task_doc, stage, level, worktree, findings):
            if stage.role == "merge":
                store.commit("task.merged", Changes().set("plan", "tasks", "T2", "status", "merged"), {"task": "T2"})
                return True, "merged", None
            return True, "ok", "sim-s"

        sup._run_stage = fake_stage

        def release_t1() -> None:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                logs = [e.get("text", "") for e in (store.read("/timeline") or [])]
                if any("waiting on sibling lease from T1" in ln for ln in logs):
                    break
                time.sleep(0.02)
            sup.tracking.release("T1", reason="abandon", abandon=True, repository=self.repo)
            sup.wake()

        t_rel = threading.Thread(target=release_t1, daemon=True)
        t_rel.start()
        sup._run_task_body("T2")
        t_rel.join(timeout=5)
        self.assertEqual(store.read("/plan/tasks/T2/status"), "merged")
        self.assertNotEqual(store.read("/plan/tasks/T2/status"), "failed")

    def test_retry_after_25_attempts_anchors_fresh_circuit_breaker_window(self) -> None:
        sup, store = _make_supervisor(
            self.tmp,
            self.repo,
            [{"id": "T1", "title": "T1", "goal": "G", "deps": [], "owns": ["src/t1.ts"], "acceptance": "true"}],
            rigor_level="vibe",
            name="retry-anchor",
        )
        self.addCleanup(store.close)
        self.addCleanup(sup.tracking.close)
        sup._task_update("T1", status="request", attempts=25, stage_stall_count=5)
        sup.control("T1", "retry")
        t_after = store.read("/plan/tasks/T1")
        self.assertEqual(t_after["retry_attempts_anchor"], 25)
        self.assertEqual(t_after["stage_stall_count"], 0)
        n = {"i": 0}

        def fake_stage(task_doc, stage, level, worktree, findings):
            if stage.role == "implement" and n["i"] == 0:
                n["i"] += 1
                return False, "one transient implement error", "sim-s"
            return True, "ok", "sim-s"

        sup._run_stage = fake_stage
        sup._run_task_body("T1")
        t_final = store.read("/plan/tasks/T1")
        self.assertEqual(t_final["attempts"], 26)
        self.assertNotEqual(t_final["status"], "request")

    def test_restore_worktree_reattaches_when_task_branch_matches_expected_head_on_detached_tree(self) -> None:
        wt_root = self.tmp / "wts-restore"
        worktree = wt.create_worktree(self.repo, "T9", self.base, wt_root)
        (worktree / "src").mkdir(parents=True, exist_ok=True)
        (worktree / "src" / "t9.ts").write_text("export const x = 9;\n", encoding="utf-8")
        tip = wt.commit_all(worktree, "T9 tip")
        assert tip is not None
        # Detach the worktree to base while leaving refs/heads/jbi/T9 at tip.
        wt.git(worktree, "checkout", "--detach", self.base)
        self.assertIsNone(wt.current_branch_or_none(worktree))
        restored = wt.restore_worktree(self.repo, "T9", self.base, wt_root, expected_head=tip)
        self.assertEqual(restored, worktree)
        self.assertEqual(wt.current_branch_or_none(restored), "jbi/T9")
        self.assertEqual(wt.head(restored, "HEAD"), tip)

    def test_refresh_linked_workspace_dists_unlinks_hardlinks_rebuilds_and_caches_by_src_sig(self) -> None:
        import os
        # Set up gitignored tg-contracts/dist in repo + a fake tsc in node_modules/.bin/tsc
        (self.repo / ".gitignore").write_text("node_modules/\ntg-contracts/dist/\n", encoding="utf-8")
        pkg = self.repo / "tg-contracts"
        (pkg / "src").mkdir(parents=True, exist_ok=True)
        (pkg / "tsconfig.build.json").write_text('{"compilerOptions": {"outDir": "dist"}}\n', encoding="utf-8")
        (pkg / "src" / "index.ts").write_text("export const v = 1;\n", encoding="utf-8")
        base2 = wt.commit_all(self.repo, "add tg-contracts")
        assert base2 is not None
        _git(self.repo, "push", "-q", "origin", "main")

        # Seed a stale hardlink source dist in repo (simulating JBI_DEPS_SOURCE)
        (pkg / "dist").mkdir(parents=True, exist_ok=True)
        stale_file = pkg / "dist" / "index.d.ts"
        stale_file.write_text("export declare const v = 0;\n", encoding="utf-8")

        worktree = wt.create_worktree(self.repo, "T-DIST", base2, self.tmp / "wts-dist")
        wt_dist = worktree / "tg-contracts" / "dist"
        wt_dist.mkdir(parents=True, exist_ok=True)
        os.link(stale_file, wt_dist / "index.d.ts")

        bin_dir = worktree / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        fake_tsc = bin_dir / "tsc"
        count_file = self.tmp / "tsc-runs.txt"
        fake_tsc.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"cf = Path({str(count_file)!r})\n"
            "cf.write_text((cf.read_text() if cf.exists() else '') + 'run\\n')\n"
            "Path('dist').mkdir(exist_ok=True)\n"
            "src = Path('src/index.ts').read_text()\n"
            "Path('dist/index.d.ts').write_text('// compiled: ' + src)\n",
            encoding="utf-8",
        )
        fake_tsc.chmod(0o755)

        rebuilt1 = wt.refresh_linked_workspace_dists(worktree)
        self.assertEqual(rebuilt1, ["tg-contracts/dist"])
        # Source repo stale file MUST NOT be mutated via hardlink!
        self.assertEqual(stale_file.read_text(encoding="utf-8"), "export declare const v = 0;\n")
        self.assertIn("export const v = 1;", (wt_dist / "index.d.ts").read_text(encoding="utf-8"))
        self.assertTrue((wt_dist / ".jbi-linked-deps").is_file())
        self.assertEqual(wt.dirty_paths(worktree), [])

        # Second call with unchanged src is a cache hit (0 extra tsc invocations)
        rebuilt2 = wt.refresh_linked_workspace_dists(worktree)
        self.assertEqual(rebuilt2, [])
        self.assertEqual(len(count_file.read_text().splitlines()), 1)

        # Modifying src invalidates signature and rebuilds dist
        (worktree / "tg-contracts" / "src" / "index.ts").write_text("export const v = 2;\n", encoding="utf-8")
        rebuilt3 = wt.refresh_linked_workspace_dists(worktree)
        self.assertEqual(rebuilt3, ["tg-contracts/dist"])
        self.assertIn("export const v = 2;", (wt_dist / "index.d.ts").read_text(encoding="utf-8"))
        self.assertEqual(len(count_file.read_text().splitlines()), 2)

    def test_sibling_lease_wait_yields_without_burning_attempts_then_widens_after_release(self) -> None:
        census_rel = "tangram-backend/nest-backend/scripts/e01-census.mjs"
        census_file = self.repo / census_rel
        census_file.parent.mkdir(parents=True, exist_ok=True)
        census_file.write_text("export default {};\n", encoding="utf-8")
        base2 = wt.commit_all(self.repo, "add census")
        assert base2 is not None
        _git(self.repo, "push", "-q", "origin", "main")

        sup, store = _make_supervisor(
            self.tmp,
            self.repo,
            [
                {"id": "T1", "title": "T1", "goal": "G1", "deps": [], "owns": [census_rel], "acceptance": "true"},
                {"id": "T2", "title": "T2", "goal": "G2", "deps": [], "owns": ["src/t2.ts"], "acceptance": "true"},
            ],
            rigor_level="vibe",
            name="sibling-wait",
        )
        self.addCleanup(store.close)
        self.addCleanup(sup.tracking.close)

        wt1 = wt.create_worktree(self.repo, "T1", base2, sup.run_dir / "worktrees")
        wt.pin_git_metadata(wt1, self.repo)
        sup._task_lease("T1", wt1)
        sup.tracking.record_commit("T1", base2)

        calls = {"n": 0}

        def fake_run_stage(task_doc, stage, level, worktree, findings):
            if stage.role == "implement":
                calls["n"] += 1
                if calls["n"] == 1:
                    return False, f"Blocked by lease scope: does not own '{census_rel}'", "sim-s"
                self.assertIn(census_rel, store.read("/plan/tasks/T2/owns"))
                return True, "ok", "sim-s"
            store.commit("task.merged", Changes().set("plan", "tasks", "T2", "status", "merged"), {"task": "T2"})
            return True, "merged", None

        sup._run_stage = fake_run_stage

        def release_t1_soon() -> None:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                logs = [e.get("text", "") for e in (store.read("/timeline") or [])]
                if any(f"waiting on sibling lease from T1 for {census_rel}" in ln for ln in logs):
                    break
                time.sleep(0.02)
            sup.tracking.release("T1", reason="abandon", abandon=True, repository=self.repo)
            sup.wake()

        releaser = threading.Thread(target=release_t1_soon, daemon=True)
        releaser.start()
        sup._run_task_body("T2")
        releaser.join(timeout=5)

        t2 = store.read("/plan/tasks/T2")
        self.assertEqual(t2["status"], "merged")
        self.assertEqual(t2.get("attempts", 0), 0, "waiting on sibling lease must not burn attempts")
        self.assertIn(census_rel, t2["owns"])
        self.assertEqual(store.read("/requests") or {}, {})
        logs = [e.get("text", "") for e in (store.read("/timeline") or [])]
        self.assertTrue(any(f"waiting on sibling lease from T1 for {census_rel}" in ln for ln in logs))


class SimulatorDefaultDisabledAndAgyBootRetryTests(unittest.TestCase):
    """Fix 5: simulator disabled by default & _probe_agy boot-race retries."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-sim-agy-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_simulator_disabled_by_default_in_initial_state_store_and_cli_seed(self) -> None:
        init = protocol.initial_state(
            "r-init", "now", name="demo", mode="new", target="/tmp/demo", max_sessions=1, lanes=1, tmux=""
        )
        self.assertFalse(init["accounts"]["simulator"]["enabled"])

        store = Store(self.tmp / "s1.db", {"run": {"id": "r1"}, "accounts": {}, "timeline": []})
        self.addCleanup(store.close)
        self.assertFalse(store.read("/accounts/simulator/enabled"))

        run_obj = cli.Run.__new__(cli.Run)
        run_obj.store = store
        run_obj._seed_accounts(offline=False, agent_provider="agy", cascade="founder")
        self.assertFalse(store.read("/accounts/simulator/enabled"))
        self.assertTrue(store.read("/accounts/agy/enabled"))
        self.assertTrue(store.read("/accounts/claude/enabled"))

        run_obj._seed_accounts(offline=True, agent_provider="agy", cascade=None)
        self.assertTrue(store.read("/accounts/simulator/enabled"))
        self.assertFalse(store.read("/accounts/agy/enabled"))

    def test_probe_agy_retries_boot_race_and_fails_after_three_retries(self) -> None:
        seq = [
            subprocess.CompletedProcess(["agy", "models"], 1, "", "daemon starting"),
            subprocess.CompletedProcess(["agy", "models"], 0, "", ""),
            subprocess.CompletedProcess(["agy", "models"], 0, "gemini-4-neon-high\n", ""),
        ]
        with mock.patch.object(providers, "_run", side_effect=seq) as run_mock, \
                mock.patch.object(providers.time, "sleep") as sleep_mock:
            res = providers._probe_agy("agy")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.models, ["gemini-4-neon-high"])
        self.assertEqual(run_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3.0), mock.call(3.0)])

        always_empty = [subprocess.CompletedProcess(["agy", "models"], 0, "", "") for _ in range(4)]
        with mock.patch.object(providers, "_run", side_effect=always_empty) as run_mock2, \
                mock.patch.object(providers.time, "sleep") as sleep_mock2:
            res2 = providers._probe_agy("agy")
        self.assertEqual(res2.status, "failed")
        self.assertEqual(run_mock2.call_count, 4)
        self.assertEqual(sleep_mock2.call_args_list, [mock.call(3.0), mock.call(3.0), mock.call(3.0)])


if __name__ == "__main__":
    unittest.main()
