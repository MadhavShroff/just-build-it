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
        sibling = self.tmp / "sibling.txt"
        with LinuxSandbox(self.repo) as fence:
            good = subprocess.run(fence.argv("printf good > src/owned.txt && mkdir -p .git/testdir"),
                                  capture_output=True, text=True)
            bad = subprocess.run(fence.argv(f"printf bad > {outside}"), capture_output=True, text=True)
            parent = subprocess.run(fence.argv(f"printf bad > {sibling}"), capture_output=True, text=True)
            temp = subprocess.run(fence.argv("mkdir -p \"$TMPDIR/scratch-task\" && printf local > \"$TMPDIR/scratch-task/x\""),
                                  capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertNotEqual(bad.returncode, 0)
        self.assertNotEqual(parent.returncode, 0)
        self.assertFalse(outside.exists())
        self.assertFalse(sibling.exists())
        self.assertTrue((self.repo / "src/owned.txt").exists())
        self.assertTrue((self.repo / ".git/testdir").is_dir())
        self.assertEqual(temp.returncode, 0)


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
