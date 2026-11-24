"""Offline tests for claude-mux-lite. They use a fake transport and isolated temp repos."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_mux_lite import harness as H
from claude_mux_lite.harness import BashGuard, HttpError, LiteHarness, Usage
from claude_mux_lite.sandbox import leased_shell


class FakeTracking:
    def __init__(self, owns):
        self.owns, self.calls = list(owns), []

    def authorize_write(self, task_id, path, *, tool_write=True):
        self.calls.append((task_id, path, tool_write))
        if not tool_write:
            return None
        if not H.path_matches(path, self.owns):
            raise RuntimeError(f"task {task_id} does not own {path!r}")
        return path


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "other").mkdir()
    (repo / "src" / "a.py").write_text("print('a')\n")
    (repo / "other" / "b.txt").write_text("b\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def reply(*blocks, usage=None, stop="end_turn"):
    out = {"content": list(blocks), "stop_reason": stop}
    if usage is not None:
        out["usage"] = usage
    return out


def text(t):
    return {"type": "text", "text": t}


def use(i, name, **inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


class Script:
    """A fake transport. It records each payload and returns scripted answers."""

    def __init__(self, *answers):
        self.answers, self.payloads = list(answers), []

    def __call__(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        item = self.answers.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cml-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo = make_repo(self.tmp)
        self.tr = FakeTracking(["src"])

    def harness(self, transport, **kw):
        kw.setdefault("sleep", lambda s: None)
        return LiteHarness(workdir=self.repo, model="m", task_id="t1", tracking=self.tr, owns=("src",),
                           transport=transport, **kw)

    def guard(self, **kw):
        return BashGuard(self.repo, ("src",), lambda p: H.path_matches(p, ["src"]), **kw)


class RequestBuilding(Base):
    def test_payload_shape_and_marks(self):
        t = Script(reply(text("done")))
        self.harness(t, repo_context="REPO", frozen_contracts="FROZEN").run("do it")
        p = t.payloads[0]
        self.assertEqual(sorted(p), ["cache_control", "max_tokens", "messages", "model", "output_config",
                                     "system", "tools"])
        self.assertEqual(p["output_config"], {"effort": "high"})
        self.assertEqual(p["cache_control"], {"type": "ephemeral"})
        self.assertEqual([x["name"] for x in p["tools"]],
                         ["bash", "read_file", "write_file", "edit_file", "list_dir", "grep_files"])
        self.assertLessEqual(H.count_marks(p) - 1, 4)

    def test_system_block_order_and_stability(self):
        a = H.build_system("REPO", "FROZEN")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["cache_control"], {"type": "ephemeral"})
        body = a[0]["text"]
        self.assertLess(body.index("six tools"), body.index("REPO"))
        self.assertLess(body.index("REPO"), body.index("FROZEN"))
        self.assertEqual(a, H.build_system("REPO", "FROZEN"))
        self.assertNotRegex(body, r"20\d\d-\d\d-\d\d")
        self.assertNotIn(str(self.repo), body)

    def test_first_message_blocks_and_later_marks(self):
        t = Script(reply(use("u1", "list_dir", path="src")), reply(text("fin")))
        h = self.harness(t)
        h.run("the brief")
        first = t.payloads[0]["messages"][0]["content"]
        self.assertEqual(len(first), 2)
        self.assertIn("cache_control", first[0])
        self.assertEqual(first[1]["text"], "Brief:\nthe brief")
        second = t.payloads[1]["messages"]
        self.assertEqual(second[-1]["content"][-1]["type"], "tool_result")
        self.assertIn("cache_control", second[-1]["content"][-1])
        for msg in second:
            if msg["role"] == "assistant":
                for block in msg["content"]:
                    self.assertNotIn("cache_control", block)
        self.assertLessEqual(H.count_marks(t.payloads[1]) - 1, 4)

    def test_no_mark_on_empty_content(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "  "}]}]
        self.assertNotIn("cache_control", H.apply_cache_marks(msgs)[0]["content"][0])


class UsageAndTransport(Base):
    def test_usage_absent_is_none_and_aggregates(self):
        t = Script(reply(use("u1", "list_dir"), usage={"input_tokens": 5, "cache_read_input_tokens": 3}),
                   reply(text("x"), usage={"input_tokens": 2, "output_tokens": 4}))
        r = self.harness(t).run("b")
        self.assertEqual(r.usage, Usage(7, 4, 3, None))
        self.assertIsNone(Usage().cache_write)
        self.assertEqual(H.parse_usage({"input_tokens": -1, "output_tokens": True}), Usage())
        self.assertEqual(H.parse_usage(None), Usage())

    def test_retry_then_success_and_exhaustion(self):
        sleeps = []
        t = Script(HttpError(429), HttpError(503), reply(text("ok")))
        r = self.harness(t, sleep=sleeps.append).run("b")
        self.assertEqual((r.exit_code, len(t.payloads), len(sleeps)), (0, 3, 2))
        t2 = Script(HttpError(500), HttpError(500), HttpError(500))
        r2 = self.harness(t2).run("b")
        self.assertEqual(r2.exit_code, 1)
        self.assertEqual(len(t2.payloads), 3)
        self.assertIn("HTTP 500", r2.final_text)

    def test_fatal_and_timeout_and_garbage(self):
        t = Script(HttpError(400, "bad"))
        self.assertEqual(self.harness(t).run("b").exit_code, 1)
        self.assertEqual(len(t.payloads), 1)
        r = self.harness(Script(TimeoutError(), TimeoutError(), TimeoutError())).run("b")
        self.assertTrue(r.timed_out)
        self.assertEqual(r.exit_code, 124)
        self.assertEqual(self.harness(Script(H.ProtocolError("invalid JSON"))).run("b").exit_code, 1)
        self.assertEqual(self.harness(Script(reply())).run("b").exit_code, 1)
        r = self.harness(Script(reply(text("no"), stop="refusal"))).run("b")
        self.assertEqual(r.exit_code, 1)
        self.assertIn("refused", r.final_text)

    def test_http_env_and_redaction(self):
        env = {"CLAUDE_MUX_BASE_URL": "http://h:1/", "CLAUDE_MUX_API_KEY": "sekret-key-1",
               "CLAUDE_MUX_HEADERS": json.dumps({"X-A": "v"})}
        tr = H.HttpTransport(env=env)
        self.assertEqual(tr.base, "http://h:1")
        self.assertEqual(tr.headers["Authorization"], "Bearer sekret-key-1")
        self.assertEqual(tr.headers["anthropic-version"], "2023-06-01")
        self.assertEqual(tr.headers["X-A"], "v")
        self.assertEqual(H.HttpTransport(env={}).base, "http://127.0.0.1:8788")
        self.assertNotIn("sekret", H.redact("bad sekret-key-1 here", ["sekret-key-1"]))
        with self.assertRaises(ValueError):
            H.HttpTransport(env={"CLAUDE_MUX_HEADERS": "[1]"})


class OperatingSystemFence(Base):
    def test_outside_worktree_write_is_absent_and_lease_write_survives(self):
        from claude_mux_lite.sandbox import LinuxSandbox
        outside = self.tmp / "external.txt"
        sibling = Path("/var/tmp") / f"jbi-sibling-{os.getpid()}.txt"
        with LinuxSandbox(self.repo) as fence:
            good = subprocess.run(fence.argv("printf good > src/owned.txt"), capture_output=True, text=True)
            metadata = subprocess.run(fence.argv("mkdir -p .git/testdir"), capture_output=True, text=True)
            bad = subprocess.run(fence.argv(f"printf bad > {outside}"), capture_output=True, text=True)
            parent = subprocess.run(fence.argv(f"printf bad > {sibling}"), capture_output=True, text=True)
            temp = subprocess.run(fence.argv("mkdir -p \"$TMPDIR/scratch-task\" && printf local > \"$TMPDIR/scratch-task/x\""),
                                  capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertNotEqual(metadata.returncode, 0)
        self.assertFalse(outside.exists())      # /tmp is a private tmpfs: the write never reaches the host
        self.assertNotEqual(parent.returncode, 0)
        self.assertFalse(sibling.exists())
        self.assertTrue((self.repo / "src/owned.txt").exists())
        self.assertFalse((self.repo / ".git/testdir").is_dir())
        self.assertEqual(temp.returncode, 0)


def linked_worktree(root: Path, repo: Path, name: str = "t1") -> Path:
    path = root / "wts" / name
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", f"jbi/{name}", str(path)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "branch", "jbi/other"], check=True, capture_output=True)
    return path


class GitFence(Base):
    """H1 and M6: the shared Git config, hooks and scratch stay out of the agent's reach."""

    def setUp(self):
        super().setUp()
        self.work = linked_worktree(self.tmp, self.repo)
        self.marker = Path("/var/tmp") / f"jbi-marker-{os.getpid()}-{self.id().split('.')[-1]}"
        self.addCleanup(lambda: self.marker.exists() and self.marker.unlink())

    def shell(self, command, work=None):
        from claude_mux_lite.sandbox import leased_shell
        target = work or self.work
        guard = BashGuard(target, ("src",), lambda p: p.startswith("src"), allow_commits=True)
        return leased_shell(guard, command)

    def test_fsmonitor_config_write_refuses_and_host_status_makes_no_marker(self):
        attack = self.shell(f"git config --local core.fsmonitor 'touch {self.marker}; false'")
        self.assertNotEqual(attack.returncode, 0)
        config = (self.repo / ".git" / "config").read_text()
        self.assertNotIn("fsmonitor", config)
        out = subprocess.run(H.host_git_argv("status", "--porcelain", work_tree=self.work),
                             capture_output=True, text=True, env=H.host_git_env())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(self.marker.exists())

    def test_host_helper_ignores_a_planted_fsmonitor_and_hook(self):
        # Plant the attack with host privileges. The helper must still not run it.
        subprocess.run(["git", "-C", str(self.work), "config", "core.fsmonitor",
                        f"touch {self.marker}; false"], check=True)
        out = subprocess.run(H.host_git_argv("status", "--porcelain", work_tree=self.work),
                             capture_output=True, text=True, env=H.host_git_env())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(self.marker.exists())

    def test_owned_git_writes_succeed_in_a_linked_worktree(self):
        done = self.shell("printf one > src/owned.txt && git add src/owned.txt && "
                          "git commit -qm one && git status --short")
        self.assertEqual(done.returncode, 0, done.stderr)
        log = subprocess.run(["git", "-C", str(self.repo), "log", "--format=%s", "jbi/t1"],
                             capture_output=True, text=True).stdout.split()
        self.assertEqual(log[0], "one")

    def test_second_task_branch_and_hooks_and_config_are_read_only(self):
        before = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "jbi/other"],
                                capture_output=True, text=True).stdout
        for command in ("git update-ref refs/heads/jbi/other HEAD",
                        "printf x > \"$(git rev-parse --git-common-dir)/hooks/pre-commit\"",
                        "git branch -f jbi/other HEAD",
                        "git config core.hooksPath /tmp"):
            done = self.shell(command)
            self.assertNotEqual(done.returncode, 0, command)
        self.assertNotIn("hooksPath", (self.repo / ".git" / "config").read_text())
        after = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "jbi/other"],
                               capture_output=True, text=True).stdout
        self.assertEqual(before, after)

    def test_plain_repository_still_commits(self):
        done = self.shell("printf one > src/z.txt && git add src/z.txt && "
                          "git commit -qm z", work=self.repo)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "log", "-1", "--format=%s"],
                                        capture_output=True, text=True).stdout.strip(), "z")

    def test_host_worktree_helper_ignores_planted_fsmonitor_and_hooks(self):
        from jbi import worktrees as wt
        subprocess.run(["git", "-C", str(self.work), "config", "core.fsmonitor",
                        f"touch {self.marker}; false"], check=True)
        hooks = self.repo / ".git" / "hooks"
        (hooks / "status").write_text(f"#!/bin/sh\ntouch {self.marker}\n")
        (hooks / "status").chmod(0o755)
        self.assertEqual(wt.dirty_paths(self.work), [])
        self.assertFalse(self.marker.exists())

    def test_unrelated_new_branch_is_refused(self):
        guard = H.BashGuard(self.work, ["src"])
        for command in ("git update-ref refs/heads/evil HEAD", "git checkout -b evil", "git branch evil"):
            self.assertIsNotNone(guard.check(command), command)
        self.assertFalse(subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--verify", "-q",
                                        "refs/heads/evil"], capture_output=True).stdout)

    def test_host_tmp_of_another_task_is_absent_and_own_scratch_works(self):
        other = Path(tempfile.mkdtemp(prefix="other-task-", dir="/tmp"))
        self.addCleanup(lambda: shutil.rmtree(other, ignore_errors=True))
        (other / "secret").write_text("host-secret")
        done = self.shell(f"cat \"$TMPDIR/{other.name}/secret\"; "
                          "printf mine > \"$TMPDIR/own\" && cat \"$TMPDIR/own\"")
        self.assertNotIn("host-secret", done.stdout)
        own = self.shell("printf mine > \"$TMPDIR/own\" && cat \"$TMPDIR/own\"")
        self.assertIn("mine", own.stdout)
        listing = self.shell("ls /tmp")
        self.assertNotIn(other.name, listing.stdout)

class NeonFenceRegressions(GitFence):
    """The Neon review proves these writes and commands must refuse or reconcile."""

    def shell_owned(self, command):
        return leased_shell(BashGuard(self.work, ("src",), lambda p: p.startswith("src"), allow_commits=True), command)

    def test_allowed_commit_and_ownership_publish_only_owned_paths(self):
        harness = LiteHarness(workdir=self.work, model="m", task_id="t1", tracking=self.tr,
                              owns=("src",), allowed_commits=True,
                              transport=Script(reply(use("b", "bash", command="printf work > src/new.txt")),
                                               reply(text("done"))))
        before = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "jbi/t1"],
                                capture_output=True, text=True).stdout
        result = harness.run("change source")
        self.assertEqual(result.exit_code, 0, result.final_text)
        self.assertEqual((self.work / "src" / "new.txt").read_text(), "work")
        after = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "jbi/t1"],
                               capture_output=True, text=True).stdout
        self.assertEqual(before, after)

    def test_allowed_git_commit_publishes_only_the_leased_ref(self):
        store = self.tr
        harness = LiteHarness(workdir=self.work, model="m", task_id="t1", tracking=store,
                              owns=("src",), allowed_commits=True,
                              transport=Script(reply(use("add", "bash", command="printf x > src/f && git add src/f")),
                                               reply(text("staged")),
                                               reply(use("commit", "bash", command="git commit -qm owned")),
                                               reply(text("committed"))))
        base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "jbi/other"],
                              capture_output=True, text=True).stdout
        deadline = harness.clock() + 5
        first = harness._t_bash({"command": "printf x > src/f && git add src/f"}, deadline)
        second = harness._t_bash({"command": "git commit -qm owned"}, deadline)
        result = harness._finish(H.HarnessResult())
        self.assertFalse(result.exit_code, first + second + result.final_text)
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "rev-parse", "jbi/other"],
                                       capture_output=True, text=True).stdout, base)
        self.assertIn("owned", subprocess.run(["git", "-C", str(self.repo), "log", "-1", "--format=%s", "jbi/t1"],
                                             capture_output=True, text=True).stdout)
        self.assertEqual(subprocess.run(["git", "-C", str(self.work), "status", "--porcelain"],
                                       capture_output=True, text=True).stdout, "")

    def guarded(self):
        return BashGuard(self.work, ("src",), lambda p: H.path_matches(p, ["src"]), allow_commits=True)

    def test_unowned_ignored_write_reconciles_and_does_not_escape(self):
        (self.work / ".gitignore").write_text("secret.env\n")
        guard = self.guarded()
        command = "printf leaked > secret.env"
        from claude_mux_lite.sandbox import LinuxSandbox
        with LinuxSandbox(self.work) as fence:
            subprocess.run(fence.argv(command), capture_output=True, check=False)
        reverted, errors = guard.reconcile()
        self.assertFalse((self.work / "secret.env").exists())
        self.assertIn("secret.env", reverted)

    def test_generalized_git_clean_and_alias_drivers_are_refused(self):
        guard = self.guarded()
        for command in ("git clean -fdx", "g=git; $g checkout jbi/other", "git clean -df -- other/file"):
            self.assertIsNotNone(guard.check(command), command)

    def test_a_preexisting_ignored_directory_survives_an_owned_write(self):
        build = self.work / "build"
        build.mkdir()
        (build / "large.bin").write_bytes(b"contents" * 600000)
        (build / "allowed.txt").write_text("old\n")
        (self.work / ".gitignore").write_text("build/\n")
        guard = BashGuard(self.work, ("build/allowed.txt",), lambda p: p == "build/allowed.txt", allow_commits=True)
        (build / "allowed.txt").write_text("new\n")
        guard.reconcile()
        self.assertTrue((build / "large.bin").exists())
        self.assertEqual((build / "allowed.txt").read_text(), "new\n")

    def test_fourth_review_drivers_and_settings_are_refused(self):
        guard = self.guarded()
        commands = (
            "git clean -fdx --", "git clean -fd --", "git clean -fdx -- src/a.py",
            "g=git; env $g checkout jbi/other", "g=git; X=1 $g stash",
            "eval 'git st''ash'", "git --no-pager -c core.hooksPath=./hooks commit -m x",
            "git --config-env=core.hooksPath=VAR commit -m x",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNotNone(guard.check(command))

    def test_fifth_review_global_options_wrappers_and_heredocs_are_refused(self):
        guard = self.guarded()
        commands = (
            "git --namespace status clean -fdx --", "git --namespace=status stash",
            "env -u FOO g''it st''ash", "timeout 5s git stash", "xargs -I X git stash",
            "bash <<'EOF'\ngit clean -fdx\nEOF", "bash <<'EOF'\ng''it st''ash\nEOF",
            "git --git-dir=./fake status", "git --exec-path=./fake status",
            "git status --namespace clean -fdx --",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNotNone(guard.check(command))
        for command in ("git --no-pager status", "git status", "git commit -qm allowed"):
            self.assertIsNone(guard.check(command), command)

    def test_global_option_driver_keeps_unowned_and_ignored_files(self):
        ignored = self.work / "build"
        ignored.mkdir()
        (ignored / "keep.txt").write_text("keep\n")
        (self.work / "untracked.txt").write_text("unowned\n")
        (self.work / ".gitignore").write_text("build/\n")
        done = self.shell_owned("git --namespace status clean -fdx --")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual((self.work / "untracked.txt").read_text(), "unowned\n")
        self.assertEqual((ignored / "keep.txt").read_text(), "keep\n")

    def test_wrapper_quoted_stash_preserves_owned_working_edits(self):
        (self.work / "src" / "a.py").write_text("preserve\n")
        done = self.shell_owned("env -u FOO g''it st''ash")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual((self.work / "src" / "a.py").read_text(), "preserve\n")

    def test_sixth_review_wrapper_and_interpreter_escapes_are_refused(self):
        (self.work / "src" / "a.py").write_text("owned edit\n")
        (self.work / "untracked.txt").write_text("unowned\n")
        evil = self.work / "evil.sh"
        evil.write_text("git clean -fdx\n")
        for command in ("timeout 5 g''it -p st''ash", "command -p bash -c \"g''it st''ash\"",
                        "/usr/bin/time g''it st''ash", "printf 'git clean -fdx\\n' | bash",
                        "bash -ec 'git clean -fdx' src/a.py", "bash evil.sh src/a.py",
                        "python3 -c \"import subprocess; subprocess.run(['git', 'clean', '-fdx'])\"",
                        "bash", "python3 script.py", "awk '{print}' src/a.py"):
            with self.subTest(command=command):
                self.assertIsNotNone(self.guarded().check(command), command)
        done = self.shell_owned("timeout 5 g''it -p st''ash")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual((self.work / "src" / "a.py").read_text(), "owned edit\n")
        piped = self.shell_owned("printf 'git clean -fdx\\n' | bash")
        self.assertNotEqual(piped.returncode, 0)
        self.assertEqual((self.work / "untracked.txt").read_text(), "unowned\n")
        inline = self.shell_owned("python3 -c \"import subprocess; subprocess.run(['git', 'clean', '-fdx'])\"")
        self.assertNotEqual(inline.returncode, 0)
        self.assertEqual((self.work / "untracked.txt").read_text(), "unowned\n")
        self.assertIsNone(self.guarded().check("bash -c 'git status'"))
        self.assertIsNone(self.guarded().check("python3 -m unittest"))

    def test_a_new_ignored_directory_cannot_delete_an_owned_leaf(self):
        (self.work / ".gitignore").write_text("build/\n")
        guard = BashGuard(self.work, ("build/allowed.txt",), lambda p: p == "build/allowed.txt", allow_commits=True)
        build = self.work / "build"
        build.mkdir()
        (build / "allowed.txt").write_text("owned\n")
        self.assertFalse(guard.reconcile()[1])
        self.assertEqual((build / "allowed.txt").read_text(), "owned\n")

    def test_a_new_ignored_directory_reconciles_owned_and_unowned_leaves(self):
        (self.work / ".gitignore").write_text("build/\n")
        guard = BashGuard(self.work, ("build/allowed.txt",), lambda p: p == "build/allowed.txt", allow_commits=True)
        build = self.work / "build"
        build.mkdir()
        (build / "allowed.txt").write_text("owned\n")
        (build / "unowned.txt").write_text("stray\n")
        reverted, errors = guard.reconcile()
        self.assertEqual(errors, [])
        self.assertTrue(any("build/unowned.txt" in item for item in reverted))
        self.assertFalse((build / "unowned.txt").exists())
        self.assertEqual((build / "allowed.txt").read_text(), "owned\n")

    def test_seventh_review_bypasses_are_refused(self):
        (self.work / "src" / "a.py").write_text("owned edit\n")
        (self.work / "untracked.txt").write_text("unowned\n")
        (self.work / "src" / "evil.py").write_text(
            "import subprocess; subprocess.run(['git', 'clean', '-fdx'])\n")
        refused = (
            "dash -c 'git clean -fdx'", 'dash -c "g\'\'it st\'\'ash"',
            "stdbuf -oL git clean -fdx", "setsid git clean -fdx",
            "taskset -c 0 git clean -fdx", "ionice git clean -fdx",
            "chrt -r 1 git clean -fdx",
            "find . -maxdepth 0 -exec git clean -fdx {} +",
            "find . -name unowned.txt -delete",
            "printf 'git clean -fdx\\n' | bash -s src/a.py",
            'python3 -c"import subprocess; subprocess.run([\'git\', \'clean\', \'-fdx\'])"',
            'perl -e"system(qw(git clean -fdx))"',
            'node --eval="require(\'child_process\').execSync(\'git clean -fdx\')"',
            "printf 'git clean -fdx\\n' | python3",
            "printf 'git clean -fdx\\n' | python3 -",
            "python3 -m src.evil",
            "busybox sh -c 'git clean -fdx'",
            "python3 -", "node", "bash -s",
        )
        for command in refused:
            with self.subTest(command=command):
                self.assertIsNotNone(self.guarded().check(command), command)
        self.assertIsNone(self.guarded().check("python3 -m unittest"))
        done = self.shell_owned("dash -c 'git clean -fdx'")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual((self.work / "untracked.txt").read_text(), "unowned\n")
        stashed = self.shell_owned('dash -c "g\'\'it st\'\'ash"')
        self.assertNotEqual(stashed.returncode, 0)
        self.assertEqual((self.work / "src" / "a.py").read_text(), "owned edit\n")
        executed = self.shell_owned("find . -maxdepth 0 -exec git clean -fdx {} +")
        self.assertNotEqual(executed.returncode, 0)
        self.assertEqual((self.work / "untracked.txt").read_text(), "unowned\n")

    def test_an_owned_subdirectory_under_a_baseline_ignored_dir_survives(self):
        build = self.work / "build"
        (build / "sub").mkdir(parents=True)
        (build / "sub" / "base.txt").write_text("baseline\n")
        (self.work / ".gitignore").write_text("build/\n")
        guard = BashGuard(self.work, ("build/owned_sub/out.txt",),
                          lambda p: p == "build/owned_sub/out.txt", allow_commits=True)
        owned = build / "owned_sub"
        owned.mkdir()
        (owned / "out.txt").write_text("owned\n")
        (build / "unowned_new.txt").write_text("new\n")
        reverted, errors = guard.reconcile()
        self.assertEqual(errors, [])
        self.assertEqual((owned / "out.txt").read_text(), "owned\n")
        self.assertEqual((build / "sub" / "base.txt").read_text(), "baseline\n")
        self.assertFalse((build / "unowned_new.txt").exists())

    def test_nested_preexisting_ignored_directories_survive_owned_writes(self):
        build = self.work / "build"
        (build / "sub").mkdir(parents=True)
        (build / "sub" / "big.bin").write_bytes(b"content" * 700000)
        (build / "allowed.txt").write_text("before\n")
        (self.work / ".gitignore").write_text("build/\n")
        guard = BashGuard(self.work, ("build/allowed.txt",), lambda p: p == "build/allowed.txt", allow_commits=True)
        (build / "allowed.txt").write_text("after\n")
        guard.reconcile()
        self.assertTrue((build / "sub" / "big.bin").is_file())
        self.assertEqual((build / "allowed.txt").read_text(), "after\n")

    def test_large_baseline_ignored_directories_keep_leaf_guard_state(self):
        path = self.work / "ignored"
        path.mkdir()
        (path / "keep.txt").write_text("preserve")
        (self.work / ".gitignore").write_text("ignored/\n")
        guard = BashGuard(self.work, ("ignored/owned.txt",), lambda p: p == "ignored/owned.txt", allow_commits=True)
        self.assertIn("ignored/keep.txt", guard.baseline)
        self.assertIn("ignored/keep.txt", guard._ignored_backup)

    def test_writes_in_a_preexisting_ignored_directory_are_reconciled(self):
        ignored = self.work / "build" / "sub"
        ignored.mkdir(parents=True)
        original = ignored / "existing.txt"
        original.write_text("keep\n")
        (self.work / ".gitignore").write_text("build/\n")
        guard = self.guarded()
        changed = ignored / "new.txt"
        from claude_mux_lite.sandbox import LinuxSandbox
        with LinuxSandbox(self.work, git_dir=guard.git_dir, common_dir=guard.common_dir) as fence:
            subprocess.run(fence.argv("printf changed > build/sub/existing.txt; printf new > build/sub/new.txt"),
                           capture_output=True, text=True)
        original.write_text("changed\n")
        changed.write_text("new\n")
        reverted, errors = guard.reconcile()
        self.assertEqual(original.read_text(), "keep\n")
        self.assertFalse(changed.exists())
        self.assertEqual(guard.reconcile()[1], [])

    def test_dynamic_git_verbs_cannot_bypass_the_precheck(self):
        guard = self.guarded()
        for command in ("a=stash; git $a", "x=update-ref; git \"$x\" refs/heads/other HEAD",
                        "git update$IGNORE-ref refs/heads/other HEAD",
                        "git -c core.fsmonitor='touch /tmp/no' status",
                        "git -c alias.x='!touch /tmp/no' x"):
            self.assertIsNotNone(guard.check(command), command)

    def test_packed_base_and_stash_and_new_sibling_refs_are_refused(self):
        base = subprocess.run(["git", "-C", str(self.repo), "pack-refs", "--all", "--prune"], check=False)
        self.assertEqual(base.returncode, 0, base.stderr)
        old = (self.repo / ".git/packed-refs").read_bytes()
        for command in ("printf '0000000000000000000000000000000000000000 refs/heads/main\\n' >> "
                        "\"$(git rev-parse --git-common-dir)/packed-refs\"",
                        "git stash", "git update-ref refs/stash HEAD",
                        "git update-ref refs/heads/jbi/another HEAD"):
            done = self.shell_owned(command)
            self.assertNotEqual(done.returncode, 0, command)
        self.assertEqual((self.repo / ".git/packed-refs").read_bytes(), old)
        self.assertFalse((self.repo / ".git/refs/stash").exists())
        self.assertFalse(subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--verify", "-q",
                                        "jbi/another"], capture_output=True).stdout)
        done = self.shell_owned("printf one > src/new.txt && git add src/new.txt && "
                                "git commit -qm correct")
        self.assertEqual(done.returncode, 0, done.stderr)


class NeonHostAndSandboxRegression(GitFence):
    def test_a_private_git_command_cannot_rewrite_the_linked_pointer(self):
        from claude_mux_lite.private_git import PrivateGit
        from claude_mux_lite.sandbox import LinuxSandbox
        from claude_mux_lite.private_git import PrivateGit
        guard = BashGuard(self.work, ("src",), lambda p: False)
        private = PrivateGit(guard, self.tmp)
        self.addCleanup(private.__exit__)
        with LinuxSandbox(self.work, git_dir=guard.git_dir, common_dir=guard.common_dir,
                          private_git=True, real_common_dir=private.real_common) as fence:
            done = subprocess.run(fence.argv("printf 'gitdir: /tmp/evil' > .git"), capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("worktrees/t1", (self.work / ".git").read_text())

    def test_two_commits_cannot_hide_an_unowned_file_then_restore_it(self):
        from claude_mux_lite.sandbox import LinuxSandbox
        from claude_mux_lite.private_git import PrivateGit
        guard = BashGuard(self.work, ("src",), lambda p: p.startswith("src"), allow_commits=True)
        private = PrivateGit(guard, self.tmp)

        self.addCleanup(private.__exit__)
        with LinuxSandbox(self.work, git_dir=guard.git_dir, common_dir=guard.common_dir,
                          private_git=True, real_common_dir=private.real_common) as fence:
            for cmd in ("cp src/a.py src/b.py; printf bad > other/b.txt; git add -A; git commit -qm bad",
                        "git checkout HEAD^ -- other/b.txt; git commit -qm good"):
                done = subprocess.run(fence.argv(cmd), env=H.fence_env(), capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
        with self.assertRaisesRegex(RuntimeError, "one private commit"):
            private.publish()

    def test_a_failed_test_does_not_invalidate_later_valid_private_commits(self):
        guard = BashGuard(self.work, ("src",), lambda p: p.startswith("src"), allow_commits=True)
        result = leased_shell(guard, "false; printf next > src/new.txt && git add src/new.txt && "
                             "git commit -qm correct")
        self.assertIn("correct", subprocess.run(["git", "-C", str(self.repo), "log", "-1", "--format=%s", "jbi/t1"],
                                               capture_output=True, text=True).stdout)
        self.assertEqual(result.returncode, 0)

    def test_polishing_one_file_does_not_overwrite_a_second_uncommitted_file(self):
        harness = LiteHarness(workdir=self.work, model="m", task_id="t1", tracking=self.tr,
                              owns=("src",), allowed_commits=True, transport=Script(reply(use("b", "bash", command="true"))))
        (self.work / "src" / "b.py").write_text("uncommitted\n")
        deadline = harness.clock() + 5
        command = "printf good > src/a.py && git add src/a.py && git commit -qm owned"
        harness._t_bash({"command": command}, deadline)
        result = harness._finish(H.HarnessResult())
        self.assertEqual(result.exit_code, 0, result.final_text)
        self.assertEqual((self.work / "src" / "b.py").read_text(), "uncommitted\n")

    def test_a_valid_delete_commit_leaves_real_index_and_tree_in_sync(self):
        from claude_mux_lite.private_git import PrivateGit
        command = "git rm src/a.py && git commit -qm delete"
        done = self.shell(command)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(subprocess.run(["git", "-C", str(self.work), "status", "--porcelain"],
                                       capture_output=True, text=True).stdout, "")
        self.assertFalse((self.work / "src" / "a.py").exists())

    def test_a_valid_in_worktree_symlink_commit_leaves_no_staged_change(self):
        done = self.shell("ln -s a.py src/link.py && ln -s src/a.py src/other.py && "
                          "git add src/link.py src/other.py && git commit -qm symlinks")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(subprocess.run(["git", "-C", str(self.work), "status", "--porcelain"],
                                       capture_output=True, text=True).stdout, "")

    def test_merge_commit_change_lists_include_every_parent(self):
        from claude_mux_lite.private_git import PrivateGit
        from claude_mux_lite.sandbox import LinuxSandbox
        guard = BashGuard(self.repo, ("src",), lambda p: p.startswith("src"), allow_commits=True)
        guard = BashGuard(self.repo, ("src",), lambda p: p.startswith("src"), allow_commits=True)
        private = PrivateGit(guard, self.tmp)
        self.addCleanup(private.__exit__)
        env = {"GIT_DIR": str(private.git_dir), "GIT_WORK_TREE": str(self.repo), "GIT_COMMON_DIR": str(private.common_dir)}
        with LinuxSandbox(self.repo, git_dir=private.git_dir, common_dir=private.common_dir,
                          scratch=self.tmp / "command", private_git=True, real_common_dir=private.real_common) as fence:
            command = "printf hidden > other/b.txt && git add other/b.txt && git commit -qm hidden"
            subprocess.run(fence.argv(command), env=env, capture_output=True, check=True)
        tree = subprocess.check_output(["git", "--git-dir", str(private.git_dir), "rev-parse",
                                        private.before + "^{tree}"]).strip().decode()
        right = private.guard._rev()
        merge = subprocess.check_output(["git", "--git-dir", str(private.git_dir), "-c", "user.name=t",
                                        "-c", "user.email=t@t", "commit-tree", tree,
                                        "-p", private.before, "-p", right, "-m", "merge"]).strip().decode()
        subprocess.run(["git", "--git-dir", str(private.git_dir), "update-ref", "refs/heads/main", merge], check=True)
        with self.assertRaisesRegex(RuntimeError, "one private commit"):
            private.publish()
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "main"]).strip().decode(),
                         private.before)

    def test_a_no_commit_private_transaction_preserves_initial_dirt(self):
        (self.work / "src" / "old.txt").write_text("preserve\n")
        from claude_mux_lite.private_git import PrivateGit
        guard = BashGuard(self.work, ("src",), lambda p: H.path_matches(p, ("src",)), allow_commits=True)
        private = PrivateGit(guard, self.tmp)
        self.addCleanup(private.__exit__)
        self.assertEqual(private.publish(), ("", []))
        self.assertEqual((self.work / "src" / "old.txt").read_text(), "preserve\n")

    def test_a_rewritten_worktree_pointer_cannot_run_a_host_filter(self):
        from jbi import worktrees as wt
        path = self.work
        git_dir, common = wt.trusted_git_metadata(path, self.repo)
        marker = self.tmp / "host-filter-ran"
        hostile = path / "fakegit"
        hostile.mkdir()
        (hostile / "config").write_text("[filter \"pwn\"]\n clean = touch " + str(marker) + "\n")
        original_config = Path(common, "config").read_text()
        with Path(common, "config").open("a") as source:
            source.write("[filter \"pwn\"]\n clean = touch " + str(marker) + "\n")
        (Path(git_dir) / "commondir").unlink()
        (Path(git_dir) / "config").write_text("[filter \"pwn\"]\n clean = touch " + str(marker) + "\n")
        (path / ".git").write_text("gitdir: fakegit\n")
        (path / ".gitattributes").write_text("other/b.txt filter=pwn\n")
        wt.trusted_git(git_dir, common, path, "add", "-A")
        self.assertFalse(marker.exists())

    def test_a_gitignored_shell_write_requires_a_lease(self):
        from jbi import tracking
        store = tracking.Tracking(self.tmp / "fleet.db")
        self.addCleanup(store.close)
        store.create_task(title="x", owns=["src"], branch="jbi/t1", worktree=self.repo,
                          base_commit="0" * 40, task_id="t1")
        (self.repo / ".gitignore").write_text("ignored\n")
        harness = LiteHarness(workdir=self.repo, model="m", task_id="t1", tracking=store, owns=("src",),
                              transport=Script(reply(use("b", "bash", command="printf x > ignored"),
                                                     reply(text("end")))))
        result = harness.run("write")
        self.assertNotEqual(result.exit_code, 0)
        # A missing lease makes the tool refusal error rather than exposing a new file to the real caller.
        tool = harness._tool_result({"name": "bash", "id": "x", "input": {"command": "printf x > ignored"}},
                                     harness.clock() + 5)
        self.assertIn("no live lease", tool["content"])
        self.assertFalse((self.repo / "ignored").exists())


class Tools(Base):
    def run_tools(self, *uses, followup="done"):
        t = Script(reply(*uses), reply(text(followup)))
        h = self.harness(t)
        r = h.run("b")
        results = t.payloads[1]["messages"][-1]["content"]
        return h, r, results, t

    def test_all_results_in_one_user_message_and_unknown_tool(self):
        _, r, results, t = self.run_tools(use("a", "list_dir", path="src"), use("b", "nope"),
                                          use("c", "read_file", path="src/a.py"))
        self.assertEqual([x["tool_use_id"] for x in results], ["a", "b", "c"])
        self.assertTrue(results[1].get("is_error"))
        self.assertIn("1: print('a')", results[2]["content"])
        self.assertEqual(r.exit_code, 0)

    def test_thinking_preserved_and_take_messages_deep_copy(self):
        think = {"type": "thinking", "thinking": "hm", "signature": "s"}
        t = Script(reply(think, use("a", "list_dir")), reply(text("fin")))
        h = self.harness(t)
        h.run("b")
        msgs = h.take_messages()
        self.assertEqual(msgs[1]["content"][0], think)
        msgs[0]["content"][0]["text"] = "mutated"
        self.assertNotEqual(h.take_messages()[0]["content"][0]["text"], "mutated")
        self.assertIn("cache_control", h.take_messages()[-1]["content"][-1])

    def test_write_edit_respect_lease(self):
        _, _, res, _ = self.run_tools(use("a", "write_file", path="src/n.py", content="x=1\n"),
                                      use("b", "write_file", path="other/n.txt", content="no"),
                                      use("c", "edit_file", path="src/a.py", old_text="'a'", new_text="'z'"))
        self.assertIn("OK", res[0]["content"])
        self.assertTrue(res[1].get("is_error"))
        self.assertFalse((self.repo / "other" / "n.txt").exists())
        self.assertEqual((self.repo / "src" / "a.py").read_text(), "print('z')\n")
        self.assertIn(("t1", "src/n.py", True), self.tr.calls)

    def test_no_lease_refuses_all_writes(self):
        self.tr.owns = []
        _, _, res, _ = self.run_tools(use("a", "write_file", path="src/n.py", content="x"))
        self.assertTrue(res[0].get("is_error"))
        self.assertFalse((self.repo / "src" / "n.py").exists())

    def test_symlink_escape_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "t.txt").write_text("keep")
        os.symlink(outside, self.repo / "src" / "link")
        os.symlink(outside / "t.txt", self.repo / "src" / "flink")
        _, _, res, _ = self.run_tools(use("a", "write_file", path="src/link/new.txt", content="x"),
                                      use("b", "write_file", path="src/flink", content="x"),
                                      use("c", "read_file", path="src/flink"),
                                      use("d", "edit_file", path="src/flink", old_text="keep", new_text="gone"))
        self.assertTrue(all(r.get("is_error") for r in res))
        self.assertFalse((outside / "new.txt").exists())
        self.assertEqual((outside / "t.txt").read_text(), "keep")

    def test_read_limits_and_list_grep(self):
        (self.repo / "src" / "big.txt").write_text("\n".join(f"line {i}" for i in range(1000)))
        (self.repo / "src" / "bin.dat").write_bytes(b"\0needle")
        (self.repo / "src" / ".hid").write_text("needle")
        _, _, res, _ = self.run_tools(use("a", "read_file", path="src/big.txt"),
                                      use("b", "grep_files", pattern="needle|print"),
                                      use("c", "list_dir", path="."))
        self.assertEqual(len(res[0]["content"].splitlines()), 400)
        self.assertLessEqual(len(res[0]["content"]), 24000)
        self.assertIn("src/a.py:1", res[1]["content"])
        self.assertNotIn("bin.dat", res[1]["content"])
        self.assertNotIn(".hid", res[1]["content"])
        self.assertNotIn(".git/", res[1]["content"])
        self.assertNotIn(".git", res[2]["content"].splitlines())

    def test_path_glob_ownership(self):
        self.assertTrue(H.path_matches("src/x/y.py", ["src"]))
        self.assertTrue(H.path_matches("src/a.py", ["src/*.py"]))
        self.assertFalse(H.path_matches("srcx/a.py", ["src"]))
        self.assertFalse(H.path_matches("src/a.txt", ["src/*.py"]))


class Sessions(Base):
    def test_warm_session_appends_and_before_run(self):
        t = Script(reply(text("one")), reply(text("two")))
        h = self.harness(t)
        h.run("first")
        seed = h.take_messages()
        calls = []
        t2 = Script(reply(text("again")))
        h2 = self.harness(t2, session=seed, before_run=lambda: calls.append(1))
        r = h2.run("second")
        self.assertEqual(calls, [1])
        self.assertEqual(r.exit_code, 0)
        msgs = t2.payloads[0]["messages"]
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["content"][0]["text"], seed[0]["content"][0]["text"])
        self.assertEqual(msgs[2]["content"][0]["text"], "Brief:\nsecond")

    def test_before_run_refusal_and_dirty_workdir(self):
        def refuse():
            raise RuntimeError("lease outside task")
        h = self.harness(Script(), session=[{"role": "user", "content": [text("x")]}], before_run=refuse)
        r = h.run("b")
        self.assertEqual(r.exit_code, 1)
        self.assertIn("lease outside task", r.final_text)
        h2 = self.harness(Script(reply(text("a")), reply(text("b"))))
        h2.run("one")
        (self.repo / "stray.txt").write_text("x")
        self.assertEqual(h2.run("two").exit_code, 1)

    def test_max_turns_and_receipt_and_checkpoint(self):
        t = Script(reply(use("a", "list_dir")), reply(use("b", "list_dir")))
        self.assertEqual(self.harness(t, max_turns=2).run("b").exit_code, 2)
        rdir = self.tmp / "receipts"
        rdir.mkdir()
        ck = self.tmp / "state" / "ck.json"
        t = Script(reply(text("BRIEF t1 | uncommitted | PASS\nDID: x")))
        h = self.harness(t, receipt=rdir / "r.txt", checkpoint=ck)
        self.assertEqual(h.run("b").exit_code, 0)
        self.assertTrue((rdir / "r.txt").read_text().startswith("BRIEF t1"))
        self.assertIn(("t1", "receipts/r.txt", False), self.tr.calls)
        self.assertEqual(len(H.load_checkpoint(ck)), 2)
        self.assertNotIn("Bearer", ck.read_text())
        with self.assertRaises(ValueError):
            self.harness(Script(), checkpoint=self.repo / "ck.json")
        t = Script(reply(text("no header")))
        self.harness(t, receipt=rdir / "r2.txt").run("b")
        self.assertFalse((rdir / "r2.txt").exists())


class Bash(Base):
    def test_fence_refuses_stash_and_escapes(self):
        g = self.guard()
        for cmd in ("git stash", "git -C . stash push", "echo x > ../o.txt", "echo x >> /tmp/o.txt",
                    "echo x | tee /tmp/o.txt", "sed -i s/a/b/ ../x", "cp src/a.py /tmp/z", "mv src/a.py ../z",
                    "mkdir -p /tmp/q", "touch ../t", "find .. -delete", "python3 -c \"open('/etc/x','w')\""):
            self.assertIsNotNone(g.check(cmd), cmd)
        for cmd in ("git status", "ls src", "echo hi > src/ok.txt", "echo hi 2>/dev/null", "python3 -m unittest"):
            self.assertIsNone(g.check(cmd), cmd)

    def test_reconcile_reverts_unowned_and_keeps_owned(self):
        g = self.guard()
        (self.repo / "other" / "b.txt").write_text("changed\n")
        (self.repo / "other" / "new.txt").write_text("n")
        (self.repo / "src" / "ok.txt").write_text("ok")
        (self.repo / "other" / "b.txt").write_text("changed\n")
        reverted, errors = g.reconcile()
        self.assertEqual(sorted(reverted), ["other/b.txt", "other/new.txt"])
        self.assertEqual(errors, [])
        self.assertEqual((self.repo / "other" / "b.txt").read_text(), "b\n")
        self.assertFalse((self.repo / "other" / "new.txt").exists())
        self.assertTrue((self.repo / "src" / "ok.txt").exists())

    def test_reconcile_restores_deleted_and_keeps_baseline_dirt(self):
        (self.repo / "mine.txt").write_text("other session")
        g = self.guard()
        os.unlink(self.repo / "other" / "b.txt")
        reverted, errors = g.reconcile()
        self.assertEqual(reverted, ["other/b.txt"])
        self.assertTrue((self.repo / "other" / "b.txt").exists())
        self.assertTrue((self.repo / "mine.txt").exists())
        self.assertEqual(errors, [])

    def test_commit_is_undone_unless_allowed(self):
        g = self.guard()
        (self.repo / "src" / "c.txt").write_text("c")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "c")
        reverted, errors = g.reconcile()
        self.assertIn("HEAD", reverted)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), g.head)
        self.assertEqual(errors, [])

    def test_status_parser(self):
        raw = b" M a b.txt\0?? n.txt\0R  new.txt\0old.txt\0"
        self.assertEqual(H.parse_status(raw), [(" M", "a b.txt", None), ("??", "n.txt", None),
                                              ("R ", "new.txt", "old.txt")])
        with self.assertRaises(H.GuardError):
            H.parse_status(b"x\0")

    def test_bash_tool_end_to_end_mutations_blocked(self):
        cmds = ["sed -i s/b/z/ other/b.txt", "echo x >> other/b.txt", "echo x | tee other/b.txt",
                "cp src/a.py other/c.py", "mv other/b.txt other/d.txt", "mkdir -p other/deep", "touch other/t",
                "find other -delete", "python3 -c \"open('other/p.txt','w').write('x')\"",
                "echo x > ../escape.txt", "git stash"]
        t = Script(reply(*[use(f"u{i}", "bash", command=c) for i, c in enumerate(cmds)]), reply(text("end")))
        self.harness(t).run("b")
        res = t.payloads[1]["messages"][-1]["content"]
        for c, r in zip(cmds, res):
            self.assertTrue(r.get("is_error"), c)
        self.assertEqual((self.repo / "other" / "b.txt").read_text(), "b\n")
        self.assertEqual(sorted(os.listdir(self.repo / "other")), ["b.txt"])
        self.assertFalse((self.tmp / "escape.txt").exists())
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")

    def test_bash_normal_work_allowed(self):
        t = Script(reply(use("a", "bash", command="git status --short; echo hi > src/h.txt; cat src/h.txt")),
                   reply(text("end")))
        self.harness(t).run("b")
        out = t.payloads[1]["messages"][-1]["content"][0]
        self.assertFalse(out.get("is_error"), out["content"])
        self.assertIn("hi", out["content"])


class Hygiene(unittest.TestCase):
    def test_imports_stdlib_only_and_no_forbidden_names(self):
        import ast
        import sys
        for name in ("__init__.py", "harness.py"):
            src = (Path(H.__file__).parent / name).read_text()
            for node in ast.walk(ast.parse(src)):
                mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                        else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
                for m in mods:
                    self.assertIn(m.split(".")[0], sys.stdlib_module_names | {"__future__"}, m)
            self.assertNotRegex(src.replace("rswarm project", ""), r"import rswarm|from rswarm")
        lic = Path(H.__file__).parent.parent / "licenses" / "RSWARM-MIT.txt"
        self.assertIn("Permission is hereby granted", lic.read_text())
        self.assertIn("rswarm", H.__doc__)


if __name__ == "__main__":
    unittest.main()
