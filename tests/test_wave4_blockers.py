"""Wave-4 merge blockers from the deep execution audit (2026-11-24).

Every arm reproduces one audited defect: C1 the symbolic `pr-<N>` job handover,
C2 the handover lock held across the CI poll, C3 the unfetched squash commit,
C4 the notice identity on the service calls, C5 the dag base/branch keys, the
origin fetch, and the hooked push, H1 the retired-model burn, H2 the four
identical reviewers, H3 the cross-provider history replay, and M2 the start
reclaim scope. The `pr-<N>` arms use the reply shapes the real ci-conductor
prints (conductor/ci-conductor: `ready` answers {"type": "queued",
"job": "pr-<N>"} near line 2189; `job` answers {"type": "failed",
"error": "no-such-job"} near line 1948), so no permissive stub can mask the
contract.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jbi import ci_adapter, dag_loader, models, protocol, providers, resume, rigor, taskkey, waves
from jbi import worktrees as wt
from jbi.ci_adapter import CIAdapter, CIAdapterError
from jbi.cli import apply_branch_flags, merge_branch_sources
from jbi.store import Changes, Store
from jbi.supervisor import Supervisor
from jbi.tracking import Tracking

EXEC = "ci-conductor"


def done(out: str = "", code: int = 0, err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, out, err)


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


def _state(repo: Path, run_id: str) -> dict:
    state = protocol.initial_state(run_id, "now", name="demo", mode="existing",
                                   target=str(repo), max_sessions=2, lanes=1, tmux="")
    state["config"]["rigor"] = "vibe"
    return state


def _git(cwd: Path, *args: str) -> None:
    """One real git call that may cross the local file protocol (clone, push, fetch).

    The host hooks are disabled for the same reason `CIAdapter.push_branch`
    disables them: this is host tooling, not an agent's push."""
    subprocess.run(["git", "-c", "protocol.file.allow=always", "-c", "core.hooksPath=/dev/null",
                    "-c", "user.name=jbi-test", "-c", "user.email=jbi@localhost",
                    "-C", str(cwd), *args],
                   check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)


# ---- the real service contract, replayed at the transport boundary -----------------
READY_REPLY = {"type": "queued", "job": "pr-42", "expect": "notice-on-completion",
               "text": "#42 at {head9} is queued. You get a notice on a merge or a failure.",
               "next": ["illm ci status 42"]}
NOSUCHJOB_REPLY = {"type": "failed", "error": "no-such-job", "retry": "no",
                   "text": "No job pr-42."}
JOB_RUNNING_REPLY = {"type": "data", "job": "20261124-101010-l1-g1",
                     "text": "prs: #42@{head9}\ndevelop abcdef123  lane lane-1  group -  gate -\n"
                             "started 2026-11-24 10:10:10  finished running"}
JOB_GREEN_REPLY = {"type": "data", "job": "20261124-101010-l1-g1",
                   "text": "prs: #42@{head9}\ndevelop abcdef123  lane lane-1  group -  gate -\n"
                           "started 2026-11-24 10:10:10  finished 2026-11-24 10:40:00\n"
                           "verdict GREEN  failed gates: none\n"
                           "merge: #42->{squash9}; stopped=\n"
                           "evidence /tmp/evidence"}


class _ConductorStub:
    """Replay the real CLI contract, including the symbolic job id gap.

    `ready` names the symbolic job `pr-<N>`; `job pr-<N>` answers `no-such-job`
    unless the test scripts a resolved record, which stands in for the server-side
    find_job fix that lives in another repository.
    """

    def __init__(self, fixture: "C1Fixture"):
        self.fixture = fixture

    def __call__(self, argv, cwd, env=None):
        fixture = self.fixture
        fixture.calls.append((list(argv), dict(env) if env else None))
        if argv[0] == EXEC:
            verb, args = argv[1], argv[2:]
            if verb == "job":
                wanted = args[0] if args else ""
                text = fixture.job_script.get(wanted)
                if text is None:
                    body = dict(NOSUCHJOB_REPLY)
                    body["text"] = f"No job {wanted}."
                    return done(json.dumps(body))
                return done(json.dumps({"type": "data", "job": wanted, "text": text}))
            if verb == "ready":
                body = dict(READY_REPLY)
                body["text"] = READY_REPLY["text"].format(head9=fixture.head[:9])
                return done(json.dumps(body))
            return done(json.dumps({"type": "refused", "reason": "unscripted", "text": "no"}))
        if argv[0] == "gh":
            if argv[1:3] == ["pr", "view"]:
                return done(json.dumps(fixture.pr_state))
            if argv[1:3] == ["pr", "list"]:
                return done("[]")
            if argv[1:3] == ["pr", "create"]:
                return done(f"https://example.test/pull/{fixture.pr}")
            return done("{}")
        if argv[0] == "git" and "rev-parse" in argv:
            # A verification read runs real git: a stub here would mask the fetch.
            return subprocess.run(argv, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL)
        return done()


def _handover(head: str, base_commit: str, job: str = "pr-42") -> ci_adapter.HandoverResult:
    return ci_adapter.HandoverResult(
        task="t1", branch="jbi/t1", pr_number=42, head_sha=head,
        reply={"job": job, "type": "queued", "bound": {"task": "t1", "base": "main",
                                                       "pr_number": 42, "head_sha": head}},
        base_commit=base_commit)


class C1Fixture(unittest.TestCase):
    """Real git repositories plus the real reply shapes; nothing stubs the contract."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-c1-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # origin: the GitHub stand-in. repo: this host's clone of it.
        self.origin = self.tmp / "origin.git"
        _git(self.tmp, "init", "-q", "--bare", str(self.origin))
        self.repo, self.base = _make_repo(self.tmp)
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "push", "-q", "origin", "main")
        _git(self.repo, "checkout", "-q", "-b", "jbi/t1")
        (self.repo / "work.txt").write_text("work\n", encoding="utf-8")
        self.head = wt.commit_all(self.repo, "work")
        _git(self.repo, "push", "-q", "origin", "jbi/t1")
        self.task = {"id": "t1", "title": "T one", "goal": "Do it", "branch": "jbi/t1"}
        self.pr = 42
        self.calls: list[tuple[list[str], dict | None]] = []
        self.job_script: dict[str, str] = {}
        self.pr_state = {"state": "OPEN", "headRefOid": self.head, "mergeCommit": {"oid": ""}}
        self.adapter = CIAdapter(runner=_ConductorStub(self))
        result = self.adapter.handover(self.repo, self.task, verb="ready")
        self.assertEqual(result.reply.get("job"), "pr-42")   # the real reply shape

    def _merge_on_origin(self) -> str:
        """Squash-merge the PR on the GitHub stand-in, exactly as the service does."""
        work_tree = self.tmp / "origin-work"
        _git(self.tmp, "clone", "-q", str(self.origin), str(work_tree))
        _git(work_tree, "checkout", "-q", "-B", "main", "origin/main")
        _git(work_tree, "merge", "--squash", "origin/jbi/t1")
        _git(work_tree, "commit", "-q", "-m", "t1: work (#42)")
        _git(work_tree, "push", "-q", "origin", "main:main")
        return wt.head(work_tree)


class SymbolicJobTest(C1Fixture):
    """C1: a queued pr-<N> handover is pending, never a failed handover."""

    def test_queued_pr_job_is_pending_not_an_error(self):
        result = _handover(self.head, self.base)
        self.assertTrue(self.adapter.pending(self.repo, result),
                        "a queued pr-<N> job read as finished and aborted the merge")
        kinds = [argv[1] for argv, _env in self.calls if argv[0] == EXEC]
        self.assertIn("job", kinds)

    def test_no_such_job_never_raises_for_a_symbolic_id(self):
        result = _handover(self.head, self.base)
        try:
            self.adapter.pending(self.repo, result)
        except CIAdapterError as error:
            self.fail(f"pending raised on the real no-such-job reply: {error}")

    def test_unresolved_lane_job_still_raises(self):
        result = _handover(self.head, self.base, job="20261124-101010-l1-g1")
        with self.assertRaises(CIAdapterError):
            self.adapter.pending(self.repo, result)

    def test_pending_flips_to_false_only_when_github_proves_the_merge(self):
        result = _handover(self.head, self.base)
        squash = self._merge_on_origin()
        self.pr_state = {"state": "MERGED", "headRefOid": self.head,
                         "mergeCommit": {"oid": squash}}
        self.assertFalse(self.adapter.pending(self.repo, result),
                         "the merge landed on GitHub and the wait must end")
        evidence = self.adapter.confirm_merge(self.repo, result)
        self.assertEqual(evidence["pr_number"], 42)
        self.assertEqual(evidence["head_sha"], self.head)
        self.assertEqual(evidence["squash_sha"], squash)

    def test_symbolic_confirm_demands_a_real_merge_not_a_queue(self):
        result = _handover(self.head, self.base)
        with self.assertRaises(CIAdapterError) as caught:
            self.adapter.confirm_merge(self.repo, result)
        self.assertIn("not merged", str(caught.exception))

    def test_symbolic_confirm_refuses_a_different_head(self):
        result = _handover(self.head, self.base)
        other = "c" * 40
        self.pr_state = {"state": "MERGED", "headRefOid": other, "mergeCommit": {"oid": other}}
        with self.assertRaises(CIAdapterError) as caught:
            self.adapter.confirm_merge(self.repo, result)
        self.assertIn("merged at head", str(caught.exception))

    def test_resolved_lane_job_keeps_the_green_contract(self):
        """When the service can resolve the job, the exact green text still decides."""
        result = _handover(self.head, self.base, job="20261124-101010-l1-g1")
        self.job_script["20261124-101010-l1-g1"] = JOB_RUNNING_REPLY["text"].format(
            head9=self.head[:9])
        self.assertTrue(self.adapter.pending(self.repo, result))
        squash = self._merge_on_origin()
        self.job_script["20261124-101010-l1-g1"] = JOB_GREEN_REPLY["text"].format(
            head9=self.head[:9], squash9=squash[:9])
        self.assertFalse(self.adapter.pending(self.repo, result))
        evidence = self.adapter.confirm_merge(self.repo, result)
        self.assertEqual(evidence["squash_sha"], squash)


class _MergeFixture(C1Fixture):
    """A supervisor whose CI adapter replays the real contract."""

    def _supervisor(self, run_id: str, tracking: Tracking | None = None,
                    config: dict | None = None) -> Supervisor:
        run_dir = self.tmp / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state = _state(self.repo, run_id)
        state["plan"] = waves.compile_plan([dict(self.task, deps=[], owns=["work.txt"],
                                                 acceptance="test -f work.txt")], 1)
        state["config"].update(config or {})
        store = Store(run_dir / "run.db", state)
        self.addCleanup(store.close)
        sup = Supervisor(store, _NoopHooks(), run_dir, self.repo, use_tmux=False,
                         tracking=tracking, ci_adapter=self.adapter)
        self.addCleanup(sup.tracking.close)
        return sup

    def _register_task(self, tracking: Tracking) -> None:
        tracking.ensure_task(task_id="t1", title="T one", goal="Do it", owns=["work.txt"],
                             branch="jbi/t1", worktree=str(self.repo),
                             base_commit=self.base, node="demo")


class HandoverLockTest(_MergeFixture):
    """C2: the CI wait never holds the handover lock."""

    def test_the_poll_never_samples_a_held_handover_lock(self):
        tracking = Tracking(self.tmp / "lock" / "fleet.db")
        self.addCleanup(tracking.close)
        sup = self._supervisor("lock", tracking=tracking)
        self._register_task(tracking)
        canned = _handover(self.head, self.base)
        lock_states: list[bool] = []

        def pending(repo, result):
            lock_states.append(sup._handover_lock.locked())
            return True
        done_wait = threading.Event()
        sup._merge_done["t1"] = done_wait
        with mock.patch.object(sup.ci_adapter, "pending", pending), \
                mock.patch.object(sup.ci_adapter, "handover", lambda *a, **k: canned):
            thread = threading.Thread(target=lambda: sup._merge("t1", "high", self.repo),
                                      daemon=True)
            thread.start()
            deadline = time.monotonic() + 5.0
            while not lock_states and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(lock_states, "the merge poll never ran")
            done_wait.set()
            thread.join(timeout=10)
        self.assertFalse(any(lock_states),
                         "a poll sampled the handover lock held: the wait serializes every lane")

    def test_ci_notice_answers_while_a_merge_poll_waits(self):
        tracking = Tracking(self.tmp / "notice-deadlock" / "fleet.db")
        self.addCleanup(tracking.close)
        sup = self._supervisor("notice-deadlock", tracking=tracking)
        self._register_task(tracking)
        tracking.record_ci("t1", pr_number=42, head_sha=self.head, base_commit=self.base,
                           service_reply={"job": "pr-42", "bound": {"task": "t1", "base": "main",
                                                                    "pr_number": 42,
                                                                    "head_sha": self.head}},
                           state="submitted")
        squash = self._merge_on_origin()
        self.pr_state = {"state": "MERGED", "headRefOid": self.head,
                         "mergeCommit": {"oid": squash}}
        answered = threading.Event()

        def notice():
            sup.ci_notice("t1", {"verdict": "green", "pr_number": 42, "job": "pr-42"})
            answered.set()
        notice_thread = threading.Thread(target=notice, daemon=True)
        waiter = threading.Thread(target=lambda: sup._merge("t1", "high", self.repo),
                                  daemon=True)
        waiter.start()
        time.sleep(0.3)
        notice_thread.start()
        self.assertTrue(answered.wait(30),
                        "ci_notice deadlocked on the handover lock the poll held")
        waiter.join(timeout=30)


class NoticeIdentityTest(C1Fixture):
    """C4: every service call carries the run's notice identity."""

    def _identity_runner(self, sink: list, legacy: bool = False):
        """Answer the full handover argv set; record only the service calls."""
        queued = json.dumps({"type": "queued", "job": "pr-42",
                             "text": f"#42 at {self.head[:9]} is queued."})

        def runner(argv, cwd, env=None):
            if argv[0] == "gh":
                if argv[1:3] == ["pr", "list"]:
                    return done("[]")
                if argv[1:3] == ["pr", "create"]:
                    return done(f"https://example.test/pull/{self.pr}")
                return done("{}")
            if argv[0] == "git":
                if "rev-parse" in argv:
                    return subprocess.run(argv, capture_output=True, text=True,
                                          stdin=subprocess.DEVNULL)
                return done()
            sink.append(dict(env) if env else (argv, cwd))
            return done(queued)
        if legacy:
            def legacy_runner(argv, cwd):
                return runner(argv, cwd, None)
            return legacy_runner
        return runner

    def test_service_calls_carry_illm_slot_and_run_id(self):
        seen_env: list[dict | None] = []
        adapter = CIAdapter(runner=self._identity_runner(seen_env),
                            env={"ILLM_SLOT": "jbi-run-9", "JBI_RUN_ID": "run-9"})
        adapter.handover(self.repo, self.task, verb="ready")
        service_calls = [env for env in seen_env if isinstance(env, dict)]
        self.assertTrue(service_calls)
        for env in service_calls:
            self.assertEqual(env.get("ILLM_SLOT"), "jbi-run-9")
            self.assertEqual(env.get("JBI_RUN_ID"), "run-9")
            self.assertNotIn("CLAUDE_MUX_API_KEY", env)

    def test_no_identity_keeps_the_legacy_two_argument_runner(self):
        calls: list = []
        CIAdapter(runner=self._identity_runner(calls, legacy=True)).handover(
            self.repo, self.task, verb="ready")
        self.assertEqual(len(calls), 1)
        argv, _cwd = calls[0]
        self.assertEqual(argv[:2], [EXEC, "ready"])


class NoticeRoutingTest(_MergeFixture):
    """C4: the supervisor names this run on its adapter's service environment."""

    def test_supervisor_names_the_run_slot_on_its_adapter(self):
        sup = self._supervisor("notice-slot")
        self.assertEqual(sup.ci_adapter.env.get("ILLM_SLOT"), "jbi-notice-slot")
        self.assertEqual(sup.ci_adapter.env.get("JBI_RUN_ID"), "notice-slot")

    def test_supervisor_honours_a_configured_notice_slot_and_url(self):
        sup = self._supervisor("notice-cfg", config={
            "ci_notice_slot": "wave-runner",
            "ci_notice_url": "http://127.0.0.1:8123/api/v1/commands"})
        self.assertEqual(sup.ci_adapter.env.get("ILLM_SLOT"), "wave-runner")
        self.assertEqual(sup.ci_adapter.env.get("JBI_CI_NOTICE_URL"),
                         "http://127.0.0.1:8123/api/v1/commands")


class PushAndFetchTest(_MergeFixture):
    """C5: the push disables the host hooks; the base refreshes from origin."""

    def test_push_branch_disables_hooks_and_optional_locks(self):
        seen: list[list[str]] = []

        def runner(argv, cwd, env=None):
            seen.append(list(argv))
            return done()
        CIAdapter(runner=runner).push_branch(self.repo, "jbi/t1")
        self.assertTrue(seen)
        self.assertIn("core.hooksPath=/dev/null", seen[0])
        self.assertIn("--no-optional-locks", seen[0])

    def test_start_execution_fast_forwards_a_stale_base_from_origin(self):
        behind = self.tmp / "behind"
        _git(self.tmp, "clone", "-q", str(self.origin), str(behind))
        _git(behind, "checkout", "-q", "-B", "main", "origin/main")
        (behind / "advanced.txt").write_text("advanced\n", encoding="utf-8")
        _git(behind, "add", "advanced.txt")
        _git(behind, "commit", "-qm", "advance origin")
        _git(behind, "push", "-q", "origin", "main:main")
        origin_main = wt.head(behind)
        self.assertNotEqual(origin_main, self.base)
        # The run targets a repo whose base branch is checked out (jbi owns it).
        wt.git(self.repo, "checkout", "-q", "main")
        wt.git(self.repo, "reset", "-q", "--hard", self.base)

        sup = self._supervisor("fetch-run")
        with mock.patch.object(Supervisor, "_schedule", lambda self: None):
            sup.start_execution()
        self.assertEqual(wt.head(self.repo, "main"), origin_main,
                         "start cut worktrees from a stale base")

    def test_a_non_fast_forward_origin_keeps_the_local_base(self):
        # Advance origin by one commit, then force-rewrite it: the rewrite is not a
        # fast-forward of the local base, so the refresh must refuse it.
        advance = self.tmp / "advance"
        _git(self.tmp, "clone", "-q", str(self.origin), str(advance))
        _git(advance, "checkout", "-q", "-B", "main", "origin/main")
        (advance / "advanced.txt").write_text("advanced\n", encoding="utf-8")
        _git(advance, "add", "advanced.txt")
        _git(advance, "commit", "-qm", "advance origin")
        _git(advance, "push", "-q", "origin", "main:main")
        diverged = self.tmp / "diverged"
        _git(self.tmp, "clone", "-q", str(self.origin), str(diverged))
        _git(diverged, "checkout", "-q", "-B", "main", "origin/main")
        _git(diverged, "reset", "-q", "--hard", "HEAD~1")
        (diverged / "other.txt").write_text("other\n", encoding="utf-8")
        _git(diverged, "add", "other.txt")
        _git(diverged, "commit", "-qm", "diverge origin")
        _git(diverged, "push", "-q", "--force", "origin", "main:main")

        sup = self._supervisor("diverge-run")
        with mock.patch.object(Supervisor, "_schedule", lambda self: None):
            sup.start_execution()
        self.assertEqual(wt.head(self.repo, "main"), self.base,
                         "a non-fast-forward origin rewrote the local base")
        timeline = sup.store.read("/timeline") or []
        notes = [str(entry.get("text") or "") for entry in timeline]
        self.assertTrue(any("holds no fast-forward" in note for note in notes),
                         "the refused fast-forward went unlogged")

    def test_start_reclaim_names_the_runs_root(self):
        seen_kwargs: list[dict] = []

        def spy(repo, **kwargs):
            seen_kwargs.append(kwargs)
            return resume.OrphanReport()
        sup = self._supervisor("reclaim-run")
        with mock.patch.object(Supervisor, "_schedule", lambda self: None), \
                mock.patch.object(resume, "reclaim_start_orphans", spy):
            sup.start_execution()
        self.assertTrue(seen_kwargs)
        expected = Path(os.environ.get("JBI_HOME", Path.home() / ".just-build-it")) / "runs"
        self.assertEqual(seen_kwargs[0].get("runs_root"), expected,
                         "start_execution reclaimed orphans without the shared runs root (M2)")


class DagBranchTest(unittest.TestCase):
    """C5: a DAG names its base and landing branch; the flags outrank the DAG."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-dagbranch-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    @staticmethod
    def _dag(**extra) -> str:
        doc = {"project": "demo", "repo": ".", **extra,
               "tasks": [{"id": "T1", "title": "A", "goal": "Write src/a.txt", "deps": [],
                          "owns": ["src/a.txt"], "acceptance": "test -f src/a.txt"}]}
        return json.dumps(doc)

    def test_a_dag_json_names_base_and_branch(self):
        doc = dag_loader.parse_dag_json(self._dag(base="develop", branch="release"))
        self.assertEqual(doc.get("base"), "develop")
        self.assertEqual(doc.get("branch"), "release")

    def test_a_dag_without_branch_keys_carries_none(self):
        doc = dag_loader.parse_dag_json(self._dag())
        self.assertNotIn("base", doc)
        self.assertNotIn("branch", doc)

    def test_flags_outrank_the_dag_and_the_dag_fills_the_gaps(self):
        base, branch = merge_branch_sources("flag-main", None, {"base": "develop", "branch": "release"})
        self.assertEqual((base, branch), ("flag-main", "release"))
        base, branch = merge_branch_sources(None, "flag-release", {"base": "develop", "branch": "release"})
        self.assertEqual((base, branch), ("develop", "flag-release"))
        base, branch = merge_branch_sources(None, None, {"base": "develop", "branch": "release"})
        self.assertEqual((base, branch), ("develop", "release"))
        base, branch = merge_branch_sources(None, None, {})
        self.assertEqual((base, branch), (None, None))

    def test_dag_branches_apply_to_the_store(self):
        repo, _base = _make_repo(self.tmp)
        wt.git(repo, "branch", "develop")
        wt.git(repo, "branch", "release")
        store = Store(self.tmp / "flags.db",
                      protocol.initial_state("r-b", "now", name="d", mode="existing",
                                             target=str(repo), max_sessions=1, lanes=1, tmux=""))
        self.addCleanup(store.close)
        base, branch = merge_branch_sources(None, None,
                                            dag_loader.parse_dag_json(self._dag(base="develop",
                                                                                branch="release")))
        apply_branch_flags(store, repo, base, branch, dry_run=False)
        self.assertEqual(store.read("/bootstrap/base_branch"), "develop")
        self.assertEqual(store.read("/bootstrap/branch"), "release")


class RetiredModelTest(unittest.TestCase):
    """H1: a retired model never starts an agent and never burns an attempt."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-retired-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.base = _make_repo(self.tmp)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def _supervisor(self, state, name: str = "sup") -> tuple[Supervisor, Store]:
        store = Store(self.run_dir / f"{name}.db", state)
        self.addCleanup(store.close)
        sup = Supervisor(store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(sup.tracking.close)
        return sup, store

    def test_permanent_error_cools_the_lane_for_the_whole_run(self):
        sup, _store = self._supervisor(_state(self.repo, "r-perm"))
        choice = models.Choice("agy", "gemini-4-neon-high", "S")
        session = type("S", (), {"status": {"exit_code": 1, "turns": 0}})()
        sup._maybe_cool_lane("T1", choice, session,
                             "no valid receipt (exit 1); agent: ERROR: model not recognized: "
                             "gemini-4-neon-high")
        self.assertEqual(sup.cooldowns.remaining("agy/gemini-4-neon-high"), float("inf"))
        self.assertTrue(sup.cooldowns.cooling("agy/gemini-4-neon-high"),
                        "a 120-second lane would thaw and re-burn attempts")

    def test_invalid_model_token_is_also_permanent(self):
        sup, _store = self._supervisor(_state(self.repo, "r-perm2"))
        choice = models.Choice("agy", "gemini-4-neon-high", "S")
        session = type("S", (), {"status": {"exit_code": 1, "turns": 0}})()
        sup._maybe_cool_lane("T1", choice, session, "invalid_model: gemini-4-neon-high")
        self.assertEqual(sup.cooldowns.remaining("agy/gemini-4-neon-high"), float("inf"))

    def test_a_permanent_failure_costs_no_attempt_and_reroutes_the_stage(self):
        state = _state(self.repo, "r-loop")
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Write src/a.txt", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "test -f src/a.txt"},
        ], 1)
        sup, store = self._supervisor(state, "loop")
        worktree = wt.create_worktree(self.repo, "T1", self.base, self.run_dir / "worktrees")
        wt.pin_git_metadata(worktree, self.repo)
        calls: list[str] = []

        def fake_run_stage(task, stage, level, worktree, findings):
            calls.append(stage.name)
            if len(calls) == 1:
                return False, ("no valid receipt (exit 1); agent: ERROR: "
                               "model not recognized: gemini-4-neon-high"), "gemini-4-neon-high"
            return True, "ok", "gemini-4-carbon-high"
        with mock.patch.object(sup, "_run_stage", fake_run_stage), \
                mock.patch.object(sup, "_merge", lambda *a, **k: True):
            sup._run_task_body("T1")
        self.assertEqual(calls[:2], ["implement", "implement"],
                         "the stage did not re-run on the next lane")
        task = store.read("/plan/tasks/T1")
        self.assertEqual(task.get("attempts", 0), 0,
                         "a retired model charged the task an attempt")
        self.assertEqual(task.get("stages", {}).get("merge", {}).get("status"), "passed")

    def test_the_reroute_bound_escalates_instead_of_spinning(self):
        state = _state(self.repo, "r-spin")
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Write src/a.txt", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "test -f src/a.txt"},
        ], 1)
        sup, store = self._supervisor(state, "spin")
        worktree = wt.create_worktree(self.repo, "T1", self.base, self.run_dir / "worktrees")
        wt.pin_git_metadata(worktree, self.repo)
        calls: list[str] = []

        def fake_run_stage(task, stage, level, worktree, findings):
            calls.append(stage.name)
            return False, "invalid_model: every model is retired", None
        with mock.patch.object(sup, "_run_stage", fake_run_stage):
            sup._run_task_body("T1")
        task = store.read("/plan/tasks/T1")
        self.assertEqual(task.get("status"), "request",
                         "every lane retired must escalate to a request")
        self.assertEqual(task.get("attempts", 0), rigor.DECISION_AFTER)
        self.assertLess(len(calls), 40, "the reroute bound did not stop the spin")

    def test_preflight_disables_a_dead_provider_and_cools_retired_models(self):
        state = _state(self.repo, "r-preflight")
        state["accounts"] = {"agy": {"label": "agy", "enabled": True, "status": "ok"},
                             "claude": {"label": "claude", "enabled": True, "status": "ok"}}
        state["config"]["provider_cascade"] = [
            {"provider": "claude", "model": "claude-opus-5-5"},
            {"provider": "agy", "model": "gemini-4-neon-high"},
            {"provider": "agy", "model": "gemini-4-carbon-high"},
        ]
        sup, store = self._supervisor(state, "preflight")
        probes = {"claude": providers.ProbeResult("failed", "claude auth status gave no JSON", []),
                  "agy": providers.ProbeResult("ok", "1 models listed", ["gemini-4-carbon-high"])}
        with mock.patch.object(Supervisor, "_schedule", lambda self: None), \
                mock.patch.object(resume, "reclaim_start_orphans",
                                  lambda *a, **k: resume.OrphanReport()), \
                mock.patch.object(providers, "probe", side_effect=lambda name: probes[name]):
            sup.start_execution()
        self.assertEqual(store.read("/accounts/claude/status"), "failed",
                         "a dead provider stayed enabled for the first task")
        self.assertEqual(sup.cooldowns.remaining("agy/gemini-4-neon-high"), float("inf"),
                         "a model the provider no longer lists was left hot")
        self.assertEqual(sup.cooldowns.remaining("agy/gemini-4-carbon-high"), 0.0,
                         "a healthy model was cooled")


class ReviewerDiversityTest(unittest.TestCase):
    """H2: the four reviewers read four ways, or the duplicate is skipped."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-reviewers-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.base = _make_repo(self.tmp)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        state = _state(self.repo, "r-rev")
        state["accounts"] = {"agy": {"label": "agy", "enabled": True, "status": "ok"},
                             "claude": {"label": "claude", "enabled": True, "status": "ok"},
                             "codex": {"label": "codex", "enabled": True, "status": "ok"}}
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Write src/a.txt", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "test -f src/a.txt"},
        ], 1)
        self.store = Store(self.run_dir / "run.db", state)
        self.addCleanup(self.store.close)
        self.sup = Supervisor(self.store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(self.sup.tracking.close)
        self.task = self.store.read("/plan/tasks/T1")

    def test_configured_reviewers_force_distinct_models(self):
        ran: list[tuple[str, str, str]] = []

        def fake_agent_stage(task, stage, worktree, findings, *, discard=True, forced_choice=None):
            choice = forced_choice or models.Choice("agy", "gemini-4-argon-high", "S")
            ran.append((stage.name, choice.provider, choice.model))
            return True, "clean", choice.model
        with mock.patch.object(self.sup, "_agent_stage", fake_agent_stage):
            ok, _detail, joined = self.sup._review_fanout(self.task, self.repo, [])
        self.assertTrue(ok)
        by_name = {name: (provider, model) for name, provider, model in ran}
        configured = models.reviewer_overrides()
        self.assertEqual(set(by_name), set(configured) | {"review-cascade"})
        models_run = [model for _p, model in by_name.values()]
        self.assertEqual(len(models_run), len(set(models_run)),
                         f"two reviewers ran the same model: {models_run}")
        for name, want in configured.items():
            self.assertEqual(by_name[name], want, f"{name} ignored its configured reviewer model")
        self.assertIn("gemini-4-carbon-high", joined)

    def test_a_duplicate_reviewer_is_skipped_and_its_record_says_so(self):
        empty_catalog = dict(models.catalog())
        empty_catalog["reviewers"] = {}
        ran: list[str] = []

        def fake_agent_stage(task, stage, worktree, findings, *, discard=True, forced_choice=None):
            ran.append(stage.name)
            return True, "clean", (forced_choice.model if forced_choice else "m")
        with mock.patch.object(models, "catalog", lambda: empty_catalog), \
                mock.patch.object(models, "reviewer_overrides", lambda: {}), \
                mock.patch.object(self.sup, "_agent_stage", fake_agent_stage):
            ok, detail, _joined = self.sup._review_fanout(self.task, self.repo, [])
        self.assertTrue(ok)
        self.assertEqual(len(ran), 1, f"four identical reviews ran: {ran}")
        stages = self.store.read("/plan/tasks/T1/stages")
        for name in ("review-ds-1", "review-ds-2", "review-ds-3"):
            self.assertEqual(stages[name]["status"], "passed")
            self.assertIn("skipped", stages[name]["detail"], f"{name} hid its skip")
        self.assertEqual(detail, "all four reviews passed")

    def test_a_configured_reviewer_on_a_dead_provider_is_never_forced(self):
        configured = dict(models.reviewer_overrides())
        configured["review-ds-1"] = ("codex", "gpt-6.1-sol")   # codex has no dispatch bridge
        ran: list[tuple[str, str]] = []

        def fake_agent_stage(task, stage, worktree, findings, *, discard=True, forced_choice=None):
            choice = forced_choice or models.Choice("agy", "gemini-4-argon-high", "S")
            ran.append((stage.name, choice.model))
            return True, "clean", choice.model
        with mock.patch.object(models, "reviewer_overrides", lambda: configured), \
                mock.patch.object(models, "usable", lambda s: ["agy", "claude"]), \
                mock.patch.object(self.sup, "_agent_stage", fake_agent_stage):
            ok, _detail, _joined = self.sup._review_fanout(self.task, self.repo, [])
        self.assertTrue(ok)
        models_run = [model for _name, model in ran]
        self.assertNotIn("gpt-6.1-sol", models_run,
                         "a dead provider's reviewer model was forced onto dispatch")


class HistoryProviderScopeTest(unittest.TestCase):
    """H3: a failover never replays one provider's transcript into another's."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-histkey-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.base = _make_repo(self.tmp)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        state = _state(self.repo, "r-hist")
        state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Write src/a.txt", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "test -f src/a.txt"},
        ], 1)
        self.store = Store(self.run_dir / "run.db", state)
        self.addCleanup(self.store.close)
        self.sup = Supervisor(self.store, _NoopHooks(), self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(self.sup.tracking.close)

    def _dispatch(self, provider: str) -> dict:
        return self.sup._stage_dispatch("s-T1-implement-1", "T1", self.repo,
                                        "some-model", attempt=0, provider=provider)

    def test_a_failover_starts_a_fresh_history_session(self):
        first = self._dispatch("claude")
        self.sup.tracking.record_session_history(
            first["history_session_id"],
            [{"role": "assistant", "content": "anthropic blocks"}])
        second = self._dispatch("agy")
        self.assertNotEqual(first["history_session_id"], second["history_session_id"],
                            "the agy dispatch resumed the claude transcript")
        self.assertEqual(second["history"], [])

    def test_the_same_provider_resumes_its_own_history(self):
        first = self._dispatch("agy")
        self.sup.tracking.record_session_history(
            first["history_session_id"],
            [{"role": "assistant", "content": "agy turn"}])
        second = self.sup._stage_dispatch("s-T1-implement-2", "T1", self.repo,
                                          "some-model", attempt=1, provider="agy")
        self.assertEqual(first["history_session_id"], second["history_session_id"])
        self.assertEqual(len(second["history"]), 1)

    def test_the_shortened_key_still_names_the_provider(self):
        """The length digest must not fold two providers into one key (H3)."""
        keys = []
        for provider in ("p" * 40 + "-claude", "p" * 40 + "-agy"):
            dispatch = self.sup._stage_dispatch("s-T1-implement-1", "T1", self.repo,
                                                "some-model", attempt=0, provider=provider)
            keys.append(dispatch["history_session_id"])
        self.assertEqual(len(set(keys)), 2, "two providers shared one shortened history key")
        self.assertTrue(all(len(key) <= 64 for key in keys))


class CacheKeyRoutingTest(unittest.TestCase):
    """H1: a cache key folds the pool and the cooldowns, like the dispatch does."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-keyroute-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.base = _make_repo(self.tmp)
        self.state = _state(self.repo, "r-key")
        self.state["accounts"] = {"agy": {"label": "agy", "enabled": True, "status": "ok"},
                                  "claude": {"label": "claude", "enabled": True, "status": "ok"}}
        self.state["config"]["provider_cascade"] = [
            {"provider": "claude", "model": "claude-opus-5-5"},
            {"provider": "agy", "model": "gemini-4-neon-high"},
        ]
        self.state["plan"] = waves.compile_plan([
            {"id": "T1", "title": "A", "goal": "Write src/a.txt", "deps": [],
             "owns": ["src/a.txt"], "acceptance": "test -f src/a.txt"},
        ], 1)

    def _key(self, cooldowns) -> str | None:
        cache = taskkey.WorkCache(self.tmp, "run")
        stage = rigor.stage("vibe", "implement")
        return cache.key_for(self.repo, self.state["plan"]["tasks"], "T1", stage,
                             base=self.base, state=self.state, cooldowns=cooldowns)

    def test_a_cooled_lane_hashes_a_different_key(self):
        cold = models.ProviderCooldowns()
        cold.mark("claude/claude-opus-5-5", 120.0)
        self.assertNotEqual(self._key(None), self._key(cold),
                            "a cooled lane read the key of the hot lane's recorded work")

    def test_key_for_accepts_the_pool_too(self):
        pool = models.ProviderPool([("agy", "gemini-4-neon-high")])
        key = self._key_with_pool(pool)
        self.assertIsInstance(key, str)

    def _key_with_pool(self, pool) -> str | None:
        cache = taskkey.WorkCache(self.tmp, "run")
        stage = rigor.stage("vibe", "implement")
        return cache.key_for(self.repo, self.state["plan"]["tasks"], "T1", stage,
                             base=self.base, state=self.state, pool=pool)


if __name__ == "__main__":
    unittest.main()
