"""Direct neutral dispatch and evidence-only merge evidence in the real DAG supervisor."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from jbi import briefs, ci_adapter, protocol, rigor, waves, worktrees as wt
from jbi.store import Store, Changes
from jbi.supervisor import Supervisor


class RecordedCI:
    """A test CI service. It records evidence but never merges a working tree."""

    def __init__(self, squash: str):
        self.squash, self.calls = squash, []

    def handover(self, repo, task, *, base, tracking):
        self.calls.append((task["id"], "ready"))
        return ci_adapter.HandoverResult(task["id"], f"jbi/{task['id']}", 901,
                                         task["commit"], {"type": "queued", "job": "J1"}, wt.head(repo, base))

    def pending(self, repo, result):
        return False

    def confirm_merge(self, repo, result, *, tracking):
        self.calls.append((result.task, "job"))
        tracking.record_ci(result.task, pr_number=result.pr_number, head_sha=result.head_sha,
                           base_commit=result.base_commit, squash_sha=self.squash,
                           green_run="J1", state="green",
                           service_reply={"type": "data", "text": "verdict GREEN"})
        tracking.mark_task_state(result.task, "merging")
        tracking.record_merge(result.task, result.pr_number, result.head_sha, self.squash, "J1")
        return {"squash_sha": self.squash}


class Hooks:
    def __init__(self):
        self.merged = []

    def task_merged(self, task, commit, patch):
        self.merged.append((task, commit))

    def bootstrap_finished(self, *args):
        pass

    wave_completed = all_merged = stopped = bootstrap_finished


class NeutralDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.run_dir = Path(self.tmp.name) / "run"
        self.repo.mkdir()
        self.run_dir.mkdir()
        wt.init_repo(self.repo)
        (self.repo / "README").write_text("base\n")
        self.base = wt.commit_all(self.repo, "base")
        task = {"id": "t1", "title": "Core", "goal": "write source", "deps": [],
                "owns": ["src/t1.txt"], "acceptance": "true", "model": "sim-l"}
        state = protocol.initial_state("r", "now", name="demo", mode="new", target=str(self.repo),
                                       max_sessions=1, lanes=1, tmux="")
        state["bootstrap"]["branch"] = "main"
        state["config"]["rigor"] = "vibe"
        state["plan"] = waves.compile_plan([{**task, "override": {"pin": {"provider": "claude", "model": "neutral"}}}], 1)
        state["accounts"]["claude"] = {"enabled": True, "status": "ok"}
        self.store = Store(self.run_dir / "run.db", state)
        self.addCleanup(lambda: self.store.close())
        self.hooks = Hooks()
        self.sup = Supervisor(self.store, self.hooks, self.run_dir, self.repo, use_tmux=False)
        self.addCleanup(self.sup.tracking.close)
        self.worktree = wt.create_worktree(self.repo, "t1", self.base, self.run_dir / "worktrees")

    def test_new_task_spec_has_fresh_history_and_a_granted_lease(self):
        leaf = self.sup._task_lease("t1", self.worktree)
        brief, receipt = briefs.write(self.run_dir, task=self.store.read("/plan/tasks/t1"),
                                     role="implement", stage="implement", attempt=0)
        path = self.sup._store_lite_spec("t1", "implement", self.worktree, "foo-model", 0, brief, receipt,
                                         self.run_dir / "sessions/s-t1-implement-1", 300)
        spec = json.loads(path.read_text())
        self.assertFalse(spec["continuation"])
        self.assertEqual(spec["owns"], ["src/t1.txt"])
        self.assertIsNone(leaf["released_at"])
        self.assertEqual(self.sup.tracking.fleet()["dispatches"][0]["continuation"], 0)

    def test_new_task_does_not_receive_other_sessions_history(self):
        self.sup._task_lease("t1", self.worktree)
        other = wt.create_worktree(self.repo, "t2", self.base, self.run_dir / "worktrees")
        self.sup.tracking.create_task(title="Other", owns=["src/other.txt"], branch="jbi/t2",
                                     worktree=other, base_commit=self.base, task_id="t2")
        self.sup.tracking.dispatch("t2", session_id="s-other-t2", owns=["src/other.txt"], branch="jbi/t2",
                                   worktree=other)
        self.sup.tracking.release("t2", reason="abandon", abandon=True, repository=self.repo)
        self.sup.tracking.record_session_history("s-other-t2", [{"role": "user", "content": "other task"}])
        brief, receipt = briefs.write(self.run_dir, task=self.store.read("/plan/tasks/t1"),
                                     role="implement", stage="implement", attempt=0)
        path = self.sup._store_lite_spec("t1", "implement", self.worktree, "model", 0, brief, receipt,
                                         self.run_dir / "sessions/s-t1-implement-1", 300)
        self.assertFalse(json.loads(path.read_text())["continuation"])

    def test_long_node_and_task_have_bounded_stable_lease_identity(self):
        task_id = "w-" + "x" * 60
        self.store.commit("fixture", Changes().replace("run", "name", "bench-" + "z" * 35))
        wt.create_worktree(self.repo, task_id, self.base, self.run_dir / "worktrees")
        self.sup.tracking.create_task(title="Large", owns=["src"], branch="jbi/" + task_id,
                                      worktree=self.run_dir / "worktrees" / task_id,
                                      base_commit=self.base, task_id=task_id)
        self.store.commit("fixture", Changes().set("plan", "tasks", task_id,
            waves.normalize({"id": task_id, "title": "Large", "goal": "Long", "deps": [], "owns": ["src"],
                             "acceptance": "true", "node": self.store.read("/run/name")})))
        first = self.sup._task_lease(task_id, self.run_dir / "worktrees" / task_id)
        self.assertRegex(first["session_id"], r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
        self.assertEqual(first["session_id"], self.sup._task_lease(task_id, self.run_dir / "worktrees" / task_id)["session_id"])

    def test_long_task_and_stage_names_have_bounded_stable_history_keys(self):
        self.store.commit("fixture", Changes().replace("run", "name", "tangram-wave4"))
        task_id = "wave4-auth-api"
        stage = "methodology-audit-index"
        self.store.commit("fixture", Changes().set("plan", "tasks", task_id,
            waves.normalize({"id": task_id, "title": "Long", "goal": "Long",
                             "deps": [], "owns": ["src/t1.txt"], "acceptance": "true", "node": "tangram-wave4"})))
        long_worktree = wt.create_worktree(self.repo, task_id, self.base, self.run_dir / "worktrees")
        self.sup._task_lease(task_id, long_worktree)
        keys = [self.sup._stage_dispatch(f"s-{task_id}-{role}-1", task_id, long_worktree, "m")
                for role in (stage, "methodology-security-audit", "implement")]
        for dispatch in keys:
            self.assertRegex(dispatch["history_session_id"], r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
        self.assertEqual(keys[0]["history_session_id"],
                         self.sup._stage_dispatch(f"s-{task_id}-{stage}-2",
                                                 task_id, long_worktree, "m")["history_session_id"])
        self.assertNotEqual(keys[0]["history_session_id"], keys[1]["history_session_id"])
        self.assertNotEqual(keys[0]["history_session_id"], keys[2]["history_session_id"])

    def test_same_task_correction_retains_history(self):
        self.sup._task_lease("t1", self.worktree)
        session = self.sup._stage_dispatch("s-t1-implement-1", "t1", self.worktree, "m", attempt=0)["session_id"]
        self.sup.tracking.record_session_history(f"h-{session}-s-t1-implement",
                                               [{"role": "user", "content": "first task"}])
        path = self.sup._store_lite_spec(
            "t1", "implement", self.worktree, "model", 1,
            *briefs.write(self.run_dir, task=self.store.read("/plan/tasks/t1"), role="implement", stage="implement", attempt=1),
            self.run_dir / "sessions/s-t1-implement-2", 300)
        spec = json.loads(path.read_text())
        self.assertTrue(spec["continuation"])
        self.assertIn("t1", spec["session_id"])
        self.assertTrue(spec["history_session_id"].endswith("-implement"))

    def test_each_stage_has_a_distinct_history_session(self):
        self.sup._task_lease("t1", self.worktree)
        implement = self.sup._stage_dispatch("s-t1-implement-1", "t1", self.worktree, "m", attempt=0)
        review = self.sup._stage_dispatch("s-t1-review-1", "t1", self.worktree, "m", attempt=0)
        self.assertNotEqual(implement["history_session_id"], review["history_session_id"])
        self.sup.tracking.record_session_history(implement["history_session_id"],
                                               [{"role": "user", "content": "first implement"}])
        correction = self.sup._stage_dispatch("s-t1-implement-2", "t1", self.worktree, "m", attempt=1)
        self.assertEqual(correction["history"][0]["content"], "first implement")
        self.assertTrue(correction["continuation"])
        self.assertFalse(review["continuation"])
        self.assertEqual(self.sup.tracking.fleet()["dispatches"][-1]["session_id"],
                         implement["session_id"])

    def test_a_notice_cannot_use_another_tasks_green_job(self):
        self.store.commit("fixture", Changes().set("plan", "tasks", "t2", {
            "id": "t2", "title": "Other", "goal": "other", "deps": [], "owns": ["src/t2.txt"],
            "acceptance": "true", "model": "sim-l", "status": "ready"}))
        self.sup.ci_adapter = type("CI", (), {"confirm_merge": lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("another task's evidence was offered to CI"))})()
        self.sup._task_lease("t1", self.worktree)
        self.sup.tracking.record_ci("t1", pr_number=901, head_sha=self.base, base_commit=self.base,
                                    state="submitted", service_reply={"job": "job-t1"})
        self.sup.ci_notice("t2", {"job": "job-t1", "verdict": "green"})
        self.assertEqual(self.sup.tracking.fleet()["merges"], [])
        self.assertNotIn("t2", self.sup._handovers)
        self.assertNotEqual(self.store.read("/plan/tasks/t2/status"), "merged")

    def test_standdown_preserves_dotfiles_that_look_owned_after_bad_normalization(self):
        self.sup._task_lease("t1", self.worktree)
        (self.worktree / "cfg").mkdir()
        (self.worktree / ".env").write_text("preserved\n")
        task = self.sup.tracking.get_task("t1")
        task["owns"] = ["env"]
        original_get = self.sup.tracking.get_task
        self.sup.tracking.get_task = lambda tid: {**original_get(tid), "owns": ["env"]}
        self.addCleanup(lambda: setattr(self.sup.tracking, "get_task", original_get))
        self.sup._stop("standdown")
        self.assertEqual((self.worktree / ".env").read_text(), "preserved\n")
        self.assertIsNone(self.sup.tracking.fleet()["leases"][0]["released_at"])
        self.assertEqual(wt.head(self.worktree, "HEAD"), self.base)

    def test_standdown_refuses_to_commit_unowned_work_and_preserves_lease(self):
        from unittest import mock
        self.sup._task_lease("t1", self.worktree)
        unowned = self.worktree / "unowned.txt"
        unowned.write_text("preserve without committing\n")
        self.sup._running["t1"] = object()
        self.sup.tmux.kill_server = lambda: None
        self.sup._wait_sessions = lambda timeout: None
        self.sup._signal_sessions = lambda sig, groups_too: None
        self.sup.hooks.stopped = lambda mode: None
        original = wt.commit_all
        calls = []
        self.addCleanup(lambda: setattr(wt, "commit_all", original))
        def tracked_commit(path, message):
            calls.append((path, message))
            return original(path, message)
        wt.commit_all = tracked_commit
        self.sup._stop("standdown")
        self.assertTrue(unowned.exists())
        self.assertEqual(wt.head(self.worktree, "HEAD"), self.base)
        self.assertIsNone(self.sup.tracking.fleet()["leases"][0]["released_at"])
        self.assertFalse(calls)

    def test_a_failed_first_attempt_does_not_resume_as_consecutive_user_messages(self):
        from unittest import mock
        from tests.test_lite_process import FakeEndpoint, SECRET, final
        with FakeEndpoint([final("BRIEF t1 | uncommitted | PASS")]) as server:
            with mock.patch.dict(os.environ, {"CLAUDE_MUX_BASE_URL": server.url, "CLAUDE_MUX_API_KEY": SECRET}):
                self.sup._task_lease("t1", self.worktree)
                stage = self.sup._stage_dispatch("s-t1-implement-1", "t1", self.worktree, "m", attempt=0)
                self.sup.tracking.record_session_history(stage["history_session_id"],
                    [{"role": "user", "content": [{"type": "text", "text": "failed first brief"}]}])
                spec_path = self.run_dir / "failed-retry-spec.json"
                spec = {"tracking": str(self.sup.tracking.path), "usage_path": str(self.run_dir / "usage.json"),
                        "workdir": str(self.worktree), "model": "m", "task_id": "t1", "owns": ["src/t1.txt"],
                        "session_id": stage["session_id"], "history_session_id": stage["history_session_id"],
                        "dispatch_id": self.sup.tracking.renew_dispatch("t1", model="m")["dispatch_id"],
                        "continuation": True, "receipt": str(self.run_dir / "receipt"),
                        "checkpoint": str(self.run_dir / "checkpoint.json"), "brief": "correct the failure"}
                spec_path.write_text(json.dumps(spec))
                from claude_mux_lite.__main__ import main
                self.assertEqual(main([str(spec_path)]), 0)
                brief = server.requests[0]["body"]["messages"][0]["content"]
                self.assertIn(r"Brief:", json.dumps(brief))
                self.assertIn(r"correct the failure", json.dumps(brief))

    def test_a_failed_tool_call_is_not_saved_as_unanswered_history(self):
        from claude_mux_lite.__main__ import _continuation_messages
        history = [{"role": "user", "content": "first"},
                   {"role": "assistant", "content": [{"type": "tool_use", "id": "u1", "name": "bash", "input": {}}]}]
        retained, note = _continuation_messages(history)
        self.assertEqual(retained, [])
        self.assertIn("unanswered tool", note)
        completed = [*history[:-1], {"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
        retained, note = _continuation_messages(completed)
        self.assertEqual(retained, completed)
        self.assertEqual(note, "")

    def test_rich_findings_do_not_change_the_stable_brief_prefix(self):
        receipt = self.run_dir / "receipt.txt"
        text = briefs.render(task=self.store.read("/plan/tasks/t1"), role="implement", stage="implement",
                             attempt=0, receipt=receipt)
        changed = briefs.render(task=self.store.read("/plan/tasks/t1"), role="implement", stage="implement",
                                attempt=1, receipt=receipt, findings=[f"finding {i}" for i in range(120)])
        self.assertTrue(changed.startswith(text.split("# Brief ", 1)[0]))
        self.assertLessEqual(len(changed.splitlines()), briefs.MAX_LINES)

    def test_supervisor_lease_is_live_before_real_neutral_execution_and_usage_survives(self):
        from tests.test_lite_process import FakeEndpoint, SECRET, final, write_call
        from unittest import mock
        call = write_call({"input_tokens": 7, "output_tokens": 2, "cache_read": 20, "cache_creation": 8})
        call["content"][0]["input"] = {"path": "src/t1.txt", "content": "hello\n"}
        usage = {"input_tokens": 7, "output_tokens": 2, "cache_read_input_tokens": 20,
                 "cache_creation_input_tokens": 8}
        self.store.commit("account.status", Changes().set("accounts", "claude", {"enabled": True, "status": "ok"}))
        task = self.store.read("/plan/tasks/t1")
        self.store.commit("task.override",
                          Changes().set("plan", "tasks", "t1", "override",
                                        {"pin": {"provider": "claude", "model": "neutral"}}), {})
        with FakeEndpoint([call, final("BRIEF t1 | uncommitted | PASS", usage)]) as server:
            with mock.patch.dict(os.environ, {"CLAUDE_MUX_BASE_URL": server.url, "CLAUDE_MUX_API_KEY": SECRET}):
                good, detail, model = self.sup._agent_stage(
                    self.store.read("/plan/tasks/t1"), rigor.stage("vibe", "implement"), self.worktree, [])
        self.assertTrue(good, detail)
        self.assertTrue((self.worktree / "src/t1.txt").is_file(), self.store.read("/sessions"))
        dispatches = self.sup.tracking.fleet()["dispatches"]
        self.assertIsNotNone(dispatches[0]["started_at"])
        self.assertEqual(len(dispatches), 2)
        self.assertEqual([d["continuation"] for d in dispatches], [0, 0])
        self.assertEqual(dispatches[-1]["verdict"], "passed")
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(self.sup.tracking.fleet()["costs"][0]["cache_read"], 20)

    def test_shim_spec_has_no_provider_credential(self):
        from tests.test_lite_process import FakeEndpoint, SECRET, final
        from unittest import mock
        key = "test-credential-c30b4fcb1240"
        spec_paths = []
        original = self.sup.tmux.open

        def stop_after_spec(name, argv):
            spec_paths.append(Path(argv[3]))
            raise RuntimeError("stop before actual launch")

        self.sup.tmux.open = stop_after_spec
        with mock.patch.dict(os.environ, {"CLAUDE_MUX_API_KEY": key}):
            with self.assertRaisesRegex(RuntimeError, "stop before"):
                self.sup.run_session(
                    "t1", "implement",
                    [__import__("sys").executable, "-m", "claude_mux_lite", "/x/spec.json"],
                    self.worktree, "claude", "m", 0)
        text = spec_paths[0].read_text()
        self.assertNotIn(key, text)
        self.assertNotIn("CLAUDE_MUX_API_KEY", json.loads(text)["env"])
        self.sup.tmux.open = original

    def test_bootstrap_receives_the_full_brief_and_releases_its_lease(self):
        from tests.test_lite_process import FakeEndpoint, SECRET, final, write_call
        from unittest import mock
        state = self.store.snapshot()[1]
        state["stack"] = {"layers": [{"layer": "runtime", "choice": "Python", "reason": "small"}]}
        self.store.commit("fixture", Changes().replace("stack", state["stack"])
                          .replace("bootstrap", "status", "running"))
        commands = json.dumps({"build": "true", "lint": "true"})
        calls = []
        for rel, content in [("pyproject.toml", "[project]\nname='tiny'\n"),
                              (".jbi/commands.json", commands)]:
            answer = write_call()
            answer["content"][0]["input"] = {"path": rel, "content": content}
            calls.append(answer)
        calls.append(final("BRIEF bootstrap | uncommitted | PASS"))
        with FakeEndpoint(calls) as server:
            with mock.patch.dict(os.environ, {"CLAUDE_MUX_BASE_URL": server.url, "CLAUDE_MUX_API_KEY": SECRET}):
                from jbi.engine import Engine
                engine = Engine(self.store, completer=None, runtime=self.sup)
                self.hooks.bootstrap_finished = engine.bootstrap_finished
                self.sup._bootstrap_safe()
        self.assertEqual(self.store.read("/bootstrap/status"), "passed")
        body = server.requests[0]["body"]["messages"][0]["content"][-1]["text"]
        self.assertIn("Python", body)
        self.assertIn(".jbi/commands.json", body)
        self.assertTrue(self.sup.tracking.fleet()["ci_submissions"] == [])
        lease = self.sup.tracking.fleet()["leases"][0]
        self.assertIsNotNone(lease["released_at"])
        parked = wt.git(self.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/jbi-parked")
        self.assertIn("bootstrap", parked)
        wt.git(self.repo, "show", parked + ":.jbi/commands.json")
        self.sup.tracking.create_task(title="Config", owns=[".gitignore"], branch="jbi/cfg",
                                     worktree=self.repo, task_id="cfg", base_commit=self.base)
        granted = self.sup.tracking.dispatch("cfg", session_id="s-cfg", owns=[".gitignore"],
                                            branch="jbi/cfg", worktree=self.repo)
        self.assertIsNotNone(granted["lease"])

    def test_neutral_tool_refuses_a_path_outside_task_ownership(self):
        from tests.test_lite_process import FakeEndpoint, SECRET, final, write_call
        from claude_mux_lite.harness import LiteHarness
        from unittest import mock
        self.sup._task_lease("t1", self.worktree)
        call = write_call()
        call["content"][0]["input"] = {"path": "outside.json", "content": "never write\n"}
        with FakeEndpoint([call, final("BRIEF t1 | uncommitted | FAIL")]) as server:
            with mock.patch.dict(os.environ, {"CLAUDE_MUX_BASE_URL": server.url, "CLAUDE_MUX_API_KEY": SECRET}):
                harness = LiteHarness(workdir=self.worktree, model="test", task_id="t1",
                                      tracking=self.sup.tracking, owns=("src/t1.txt",))
                result = harness.run("Write outside.json.")
        self.assertFalse((self.worktree / "outside.json").exists())
        messages = server.requests[1]["body"]["messages"]
        self.assertIn("does not own", json.dumps(messages[2]["content"]))

    def test_resume_restores_the_recorded_parked_work(self):
        self.sup._task_lease("t1", self.worktree)
        (self.worktree / "src").mkdir()
        (self.worktree / "src/t1.txt").write_text("preserve this\n")
        head = wt.commit_all(self.worktree, "t1: stand-down WIP")
        self.sup.tracking.mark_task_state("t1", "paused")
        self.sup.tracking.record_commit("t1", head)
        result = self.sup.tracking.release("t1", reason="abandon", abandon=True, repository=self.repo)
        self.assertFalse(wt.head(self.repo, "jbi/t1"))
        self.assertFalse(wt.git(self.worktree, "symbolic-ref", "-q", "--short", "HEAD", check=False))
        restored = wt.restore_worktree(self.repo, "t1", self.base, self.run_dir / "worktrees",
                                      expected_head=head)
        self.assertEqual(restored, self.worktree)
        self.assertEqual(wt.head(self.repo, "jbi/t1"), head)
        self.assertEqual((self.worktree / "src/t1.txt").read_text(), "preserve this\n")
        self._task_lease = self.sup._task_lease("t1", self.worktree)
        self.assertIsNone(self._task_lease["released_at"])

    def test_release_uses_runtime_pinned_metadata_not_an_agent_pointer(self):
        new_repo = Path(self.tmp.name) / "my_real_project"
        self.repo.rename(new_repo)
        self.repo = new_repo
        for record in (self.repo / ".git" / "worktrees").iterdir():
            if record.is_dir() and not record.is_symlink():
                pointer = record / "gitdir"
                pointer.write_text(pointer.read_text().replace(str(Path(self.tmp.name) / "repo") + "/",
                                                                str(new_repo) + "/"))
        self.sup._repo = self.repo
        wt._RUNTIME_GIT.clear()
        wt.pin_git_metadata(self.worktree, self.repo)
        (self.worktree / "src").mkdir()
        (self.worktree / "src" / "t1.txt").write_text("owned\n")
        head = wt.commit_all(self.worktree, "owned")
        self.sup._task_lease("t1", self.worktree)
        self.sup.tracking.mark_task_state("t1", "paused")
        self.sup.tracking.record_commit("t1", head)
        self.sup.tracking.release("t1", reason="abandon", abandon=True, repository=self.repo)
        self.worktree = wt.restore_worktree(self.repo, "t1", self.base, self.run_dir / "worktrees", expected_head=head)
        self.sup._task_lease("t1", self.worktree)
        wt._RUNTIME_GIT.clear()
        (self.worktree / "src").mkdir(exist_ok=True)
        (self.worktree / ".git").write_text("gitdir: fakegit\n")
        (self.worktree / "unowned.txt").write_text("preserve\n")
        hostile = self.worktree / "fakegit"
        hostile.mkdir()
        (hostile / "config").write_text("[filter \"pwn\"]\n clean = touch runtime-marker\n [core]\n fsmonitor = false\n")
        seen = []
        real = wt.trusted_git
        def record(git_dir, common_dir, workdir, *args, check=True):
            seen.append((Path(git_dir), Path(common_dir), args))
            return real(git_dir, common_dir, workdir, *args, check=check)
        from jbi import tracking
        tracking.trusted_git = record
        self.addCleanup(lambda: setattr(tracking, "trusted_git", real))
        from jbi.tracking import LeaseError
        with self.assertRaises(LeaseError):
            self.sup.tracking.release("t1", reason="abandon", abandon=True, repository=self.repo)
        self.assertTrue(seen)
        self.assertTrue(all(entry[0] != hostile for entry in seen))
        self.assertFalse(Path("runtime-marker").exists())
        self.assertTrue(any(lease["released_at"] is None for lease in self.sup.tracking.fleet()["leases"]
                            if lease["task_id"] == "t1"))

    def test_restart_release_refuses_a_fake_git_directory_and_keeps_the_lease(self):
        from jbi.tracking import LeaseError
        self.sup._task_lease("t1", self.worktree)
        self.sup.tracking.mark_task_state("t1", "paused")
        self.sup.tracking.record_commit("t1", self.base)
        wt.unpin_git_metadata(self.worktree)
        self.worktree.joinpath(".git").unlink()
        self.worktree.joinpath(".git").mkdir()
        with self.assertRaises(LeaseError):
            self.sup.tracking.release("t1", reason="abandon", abandon=True, repository=self.repo)
        self.assertIsNone(self.sup.tracking.fleet()["leases"][0]["released_at"])
        self.assertEqual(wt.head(self.repo, "jbi/t1"), self.base)

    def test_dirty_green_ci_preserves_base_live_lease_and_file(self):
        from tests.test_runtime import FakeCI
        service = FakeCI(self.repo, self.run_dir / "ci")
        self.sup.ci_adapter = service.adapter
        self.sup.ci_poll_interval = 0.01
        self.sup._task_lease("t1", self.worktree)
        (self.worktree / "src").mkdir(exist_ok=True)
        (self.worktree / "src/t1.txt").write_text("verified\n")
        head = wt.commit_all(self.worktree, "t1: verified")
        self.sup._task_update("t1", commit=head)
        self.sup.tracking.record_commit("t1", head)
        (self.worktree / "untracked.tmp").write_text("preserve\n")
        self.assertFalse(self.sup._merge("t1", "vibe", self.worktree))
        self.assertEqual(wt.head(self.repo, "main"), self.base)
        self.assertEqual(self.store.read("/plan/tasks/t1/status"), "merging")
        self.assertIsNone(next(row for row in self.sup.tracking.fleet()["leases"]
                               if row["task_id"] == "t1")["released_at"])
        self.assertEqual((self.worktree / "untracked.tmp").read_text(), "preserve\n")
        (self.worktree / "untracked.tmp").unlink()
        self.assertTrue(self.sup._merge("t1", "vibe", self.worktree))
        self.assertEqual(self.store.read("/plan/tasks/t1/status"), "merged")
        self.assertNotEqual(wt.head(self.repo, "main"), self.base)
        self.assertEqual(len([call for call in service.calls if "ready" in call]), 1)

    def test_duplicate_green_notice_keeps_one_merge_and_one_notice(self):
        from tests.test_runtime import FakeCI
        service = FakeCI(self.repo, self.run_dir / "ci")
        self.sup.ci_adapter = service.adapter
        self.sup.ci_poll_interval = 0.01
        (self.worktree / "src").mkdir()
        (self.worktree / "src/t1.txt").write_text("one\n")
        head = wt.commit_all(self.worktree, "t1")
        self.sup._task_lease("t1", self.worktree)
        self.sup._task_update("t1", commit=head)
        self.sup.tracking.record_commit("t1", head)
        self.assertTrue(self.sup._merge("t1", "vibe", self.worktree))
        self.assertEqual(len(self.sup.tracking.fleet()["merges"]), 1)
        self.sup.ci_notice("t1", {"job": "job-901-1", "verdict": "green"})
        self.assertEqual(len(self.sup.tracking.fleet()["merges"]), 1)
        self.assertEqual(self.hooks.merged, [("t1", self.store.read("/plan/tasks/t1/commit"))])
        self.assertEqual(self.store.read("/plan/tasks/t1/status"), "merged")

    def test_parked_ref_of_a_dependency_does_not_interfere_with_new_task(self):
        self.sup._task_lease("t1", self.worktree)
        self.sup.tracking.mark_task_state("t1", "paused")
        self.sup.tracking.release("t1", reason="abandon", abandon=True, repository=self.repo)
        other = wt.restore_worktree(self.repo, "other", self.base, self.run_dir / "worktrees")
        self.assertEqual(wt.head(other, "HEAD"), self.base)
        self.assertEqual(wt.git(other, "symbolic-ref", "-q", "--short", "HEAD"), "jbi/other")

    def test_merge_does_not_reuse_another_tasks_green_commit(self):
        self.store.commit("fixture", Changes().set("plan", "tasks", "t2", {
            "id": "t2", "title": "Other", "goal": "other", "deps": [], "owns": ["src/t2.txt"],
            "acceptance": "true", "model": "sim-l", "status": "ready", "commit": self.base}))
        self.sup._task_lease("t1", self.worktree)
        self.sup.tracking.record_ci("t1", pr_number=901, head_sha=self.base, base_commit=self.base,
                                    service_reply={"job": "job1"})
        self.sup.ci_adapter = type("CI", (), {
            "handover": lambda repo, task, *args, **kw: ci_adapter.HandoverResult(
                "t2", "jbi/t2", 902, self.base, {"job": "job2"}, self.base),
            "pending": lambda *_: False,
            "confirm_merge": lambda repo, result, *args, **kw: (_ for _ in ()).throw(
                AssertionError("old evidence reused; new direct proof required"))})()
        with self.assertRaisesRegex(AssertionError, "new direct proof required"):
            self.sup._merge("t2", "vibe", self.worktree)
        self.assertEqual(self.sup.tracking.fleet()["merges"], [])

    def test_direct_ci_marks_verified_merge_only(self):
        self.sup._task_lease("t1", self.worktree)
        (self.worktree / "src").mkdir()
        (self.worktree / "src/t1.txt").write_text("hello\n")
        head = wt.commit_all(self.worktree, "t1")
        self.sup._task_update("t1", commit=head)
        self.sup.tracking.record_commit("t1", head)
        self.sup.ci_adapter = RecordedCI(self.base)
        self.assertTrue(self.sup._merge("t1", "vibe", self.worktree))
        self.assertEqual(self.hooks.merged, [("t1", self.base)])
        self.assertTrue(self.sup.tracking.fleet()["merges"])
        self.sup._reclaim_worktree("t1", self.worktree)
        self.assertTrue(self.sup.tracking.fleet()["leases"][0]["released_at"])

    def test_ci_resume_reuses_exact_pending_handover(self):
        from tests.test_runtime import FakeCI
        service = FakeCI(self.repo, self.run_dir / "fake-service")
        self.sup.ci_adapter = service.adapter
        self.sup._task_lease("t1", self.worktree)
        (self.worktree / "src").mkdir(exist_ok=True)
        (self.worktree / "src/t1.txt").write_text("waiting\n")
        head = wt.commit_all(self.worktree, "t1: ready")
        self.sup._task_update("t1", commit=head)
        self.sup.tracking.record_commit("t1", head)
        handover = self.sup.ci_adapter.handover(self.repo, self.store.read("/plan/tasks/t1"),
                                                base="main", tracking=self.sup.tracking)
        self.sup._handovers["t1"] = handover
        report = service.jobs[handover.reply["job"]]
        self.assertIsNone(report["squash"])
        self.assertEqual(self.store.read("/plan/tasks/t1/status"), "ready")
        self.store.close()
        from jbi import cli
        self.store = Store(self.run_dir / "run.db")
        task = self.store.read("/plan/tasks/t1")
        self.store.commit("task.stage", Changes().set("plan", "tasks", "t1", "stages",
                          {"implement": {"status": "passed"}}).set("plan", "tasks", "t1", "status", "merging"))
        cli.prepare_resume(self.store)
        resumed = Supervisor(self.store, self.hooks, self.run_dir, self.repo, use_tmux=False,
                             ci_adapter=service.adapter)
        self.addCleanup(resumed.tracking.close)
        original = report["squash"]
        self.assertIsNone(original)
        resumed.start_execution()
        deadline = __import__("time").monotonic() + 15
        while __import__("time").monotonic() < deadline:
            record = resumed.tracking.fleet()["tasks"][0]
            if record["state"] == "merged":
                break
            __import__("time").sleep(0.1)
        self.assertEqual(record["state"], "merged")
        ready_calls = [argv for argv in service.calls if "ready" in argv]
        self.assertEqual(len(ready_calls), 1, ready_calls)
        self.assertEqual(len(resumed.tracking.fleet()["dispatches"]), 1)
        deadline = __import__("time").monotonic() + 15
        while __import__("time").monotonic() < deadline:
            lease = resumed.tracking.fleet()["leases"][0]
            if lease["released_at"] or self.store.read("/plan/tasks/t1/lease_state") == "held":
                break
            __import__("time").sleep(0.1)
        self.assertIsNotNone(lease["released_at"],
                             self.store.read("/plan/tasks/t1/release_error"))
        resumed.stop("kill")


if __name__ == "__main__":
    unittest.main()
