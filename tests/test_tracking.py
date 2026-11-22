"""Tests for the internal tracking store and its lease lifecycle."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from jbi import worktrees as wt
from jbi.tracking import LeaseError, Tracking, normalize_path, paths_overlap

TABLES = ["ci_submissions", "costs", "dispatches", "leases", "merges", "messages", "sessions", "tasks"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-tracking-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        wt.init_repo(self.repo)
        (self.repo / "README").write_text("base\n")
        self.base = wt.commit_all(self.repo, "base")
        self.db_path = self.tmp / "state" / "fleet.db"
        self.tr = Tracking(self.db_path)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.tr.close()
        except Exception:
            pass

    def make_task(self, name="t1", owns=("src/a",)):
        path = wt.create_worktree(self.repo, name, "main", self.tmp / "wts")
        self.tr.create_task(title=name, owns=list(owns), branch=f"jbi/{name}", worktree=path,
                            base_commit=self.base, task_id=name, node="n1")
        return path

    def dispatch(self, name="t1", owns=("src/a",), session="s1"):
        path = self.tmp / "wts" / name
        return self.tr.dispatch(name, session_id=session, owns=list(owns), branch=f"jbi/{name}",
                                worktree=path)

    def commit_in(self, path, name="f.txt"):
        (path / name).write_text("work\n")
        sha = wt.commit_all(path, "work")
        self.tr.record_commit(path.name, sha)
        return sha


class Lifecycle(Base):
    def test_schema_and_permissions(self):
        self.assertEqual(self.tr.table_names(), TABLES)
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path).st_mode), 0o600)
        fk = self.tr._db.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(fk, 1)

    def test_full_lifecycle_merged(self):
        path = self.make_task()
        res = self.dispatch()
        self.assertEqual(res["lease"]["owns"], ["src/a"])
        sha = self.commit_in(path)
        head = sha
        self.tr.record_ci("t1", pr_number=7, head_sha=head, base_commit=self.base,
                          service_reply={"ok": True}, green_run="run-1", squash_sha=head, state="green")
        self.tr.record_merge("t1", 7, head, head, "run-1")
        out = self.tr.release("t1", reason="merged")
        self.assertTrue(out["parked_ref"].startswith("jbi-parked/t1-"))
        self.assertEqual(wt.git(path, "rev-parse", "HEAD"), self.base)
        self.assertEqual(wt.git(path, "symbolic-ref", "-q", "HEAD", check=False), "")
        self.assertTrue(wt.head(self.repo, out["parked_ref"]))
        self.assertIsNone(wt.head(self.repo, "jbi/t1"))
        fleet = self.tr.fleet()
        self.assertEqual(fleet["tasks"][0]["state"], "merged")
        self.assertEqual(len(fleet["merges"]), 1)
        self.assertEqual(fleet["sessions"][0]["status"], "idle")
        self.assertIsNotNone(fleet["leases"][0]["released_at"])

    def test_abandon_then_redispatch_needs_release_first(self):
        path = self.make_task()
        self.dispatch()
        with self.assertRaises(LeaseError) as cm:
            self.dispatch()
        self.assertIn("live lease", str(cm.exception))
        self.commit_in(path)
        self.tr.release("t1", reason="abandon", abandon=True)
        self.assertEqual(self.tr.fleet()["tasks"][0]["state"], "abandoned")

    def test_restart_keeps_state(self):
        self.make_task()
        self.dispatch()
        self.tr.close()
        again = Tracking(self.db_path)
        self.addCleanup(again.close)
        self.assertIsNone(again.authorize_write("t1", ".jbi/scratch", tool_write=False))
        self.assertEqual(again.authorize_write("t1", "src/a/x.py"), "src/a/x.py")
        self.assertEqual(len(again.fleet()["leases"]), 1)

    def test_dispatch_checks_task_record(self):
        self.make_task()
        path = self.tmp / "wts" / "t1"
        with self.assertRaises(LeaseError):
            self.tr.dispatch("nope", session_id="s1", owns=["src/a"], branch="jbi/t1", worktree=path)
        with self.assertRaises(LeaseError):
            self.tr.dispatch("t1", session_id="s1", owns=["src/b"], branch="jbi/t1", worktree=path)
        with self.assertRaises(LeaseError):
            self.tr.dispatch("t1", session_id="s1", owns=["src/a"], branch="jbi/other", worktree=path)
        with self.assertRaises(LeaseError):
            self.tr.dispatch("t1", session_id="s1", owns=["src/a"], branch="jbi/t1", worktree=self.tmp)
        self.assertEqual(self.tr.fleet()["sessions"], [])
        self.assertEqual(self.tr.fleet()["leases"], [])

    def test_bad_ids_refused(self):
        with self.assertRaises(LeaseError):
            self.tr.create_task(title="x", owns=["a"], branch="b", worktree=self.tmp,
                                base_commit=self.base, task_id="1bad id")
        self.make_task()
        with self.assertRaises(LeaseError):
            self.tr.dispatch("t1", session_id="bad session", owns=["src/a"], branch="jbi/t1",
                             worktree=self.tmp / "wts" / "t1")


class Guards(Base):
    def test_write_without_lease_refused(self):
        self.make_task()
        with self.assertRaises(LeaseError):
            self.tr.authorize_write("t1", "src/a/x.py")
        with self.assertRaises(LeaseError):
            self.tr.authorize_write("missing", "src/a/x.py", tool_write=False)

    def test_bad_paths_refused(self):
        self.make_task()
        self.dispatch()
        for bad in ("", "/etc/passwd", "../x", "a/../../x", ".", "a\x00b", "  "):
            with self.assertRaises(LeaseError, msg=repr(bad)):
                self.tr.authorize_write("t1", bad)
        for bad in ("", "/abs", "..", "a/../b/..", "."):
            with self.assertRaises(LeaseError, msg=repr(bad)):
                self.tr.create_task(title="x", owns=[bad], branch="b", worktree=self.tmp,
                                    base_commit=self.base)

    def test_path_normalization(self):
        self.assertEqual(normalize_path("a//b/./c/"), "a/b/c")

    def test_owns_match_prefix_and_glob(self):
        self.make_task(owns=("src/a", "docs/*.md"))
        self.dispatch(owns=("src/a", "docs/*.md"))
        self.assertEqual(self.tr.authorize_write("t1", "src/a"), "src/a")
        self.assertEqual(self.tr.authorize_write("t1", "src/a/deep/x.py"), "src/a/deep/x.py")
        self.assertEqual(self.tr.authorize_write("t1", "docs/g.md"), "docs/g.md")
        for outside in ("src/ab/x", "src/b", "docs/g.txt"):
            with self.assertRaises(LeaseError):
                self.tr.authorize_write("t1", outside)

    def test_conflict_names_holder(self):
        self.make_task("t1", owns=("src/a",))
        self.make_task("t2", owns=("src/a/sub",))
        self.dispatch("t1")
        with self.assertRaises(LeaseError) as cm:
            self.dispatch("t2", owns=("src/a/sub",), session="s2")
        msg = str(cm.exception)
        for part in ("t2", "s2", "src/a/sub", "src/a", "t1", "s1"):
            self.assertIn(part, msg)

    def test_wildcard_conflict(self):
        self.make_task("t1", owns=("src/*.py",))
        self.make_task("t2", owns=("src/deep/x.py",))
        self.make_task("t3", owns=("lib/x.py",))
        self.dispatch("t1", owns=("src/*.py",))
        with self.assertRaises(LeaseError):
            self.dispatch("t2", owns=("src/deep/x.py",), session="s2")
        self.dispatch("t3", owns=("lib/x.py",), session="s3")
        self.assertTrue(paths_overlap("a/*", "a/b/c"))
        self.assertFalse(paths_overlap("a/b", "a/c"))

    def test_dirty_release_refused_lease_stays(self):
        path = self.make_task()
        self.dispatch()
        self.commit_in(path)
        (path / "f.txt").write_text("edited\n")
        with self.assertRaises(LeaseError) as cm:
            self.tr.release("t1", reason="abandon", abandon=True)
        self.assertIn("dirty", str(cm.exception))
        self.assertEqual(self.tr.authorize_write("t1", "src/a/x"), "src/a/x")
        self.assertEqual((path / "f.txt").read_text(), "edited\n")
        self.assertEqual(wt.current_branch(path), "jbi/t1")

    def test_untracked_release_refused(self):
        path = self.make_task()
        self.dispatch()
        self.commit_in(path)
        (path / "new.txt").write_text("x\n")
        with self.assertRaises(LeaseError):
            self.tr.release("t1", reason="abandon", abandon=True)
        self.assertTrue((path / "new.txt").exists())
        self.assertIsNone(self.tr.fleet()["leases"][0]["released_at"])

    def test_wrong_branch_release_refused(self):
        path = self.make_task()
        self.dispatch()
        sha = self.commit_in(path)
        wt.git(path, "checkout", "-q", "--detach", sha)
        with self.assertRaises(LeaseError):
            self.tr.release("t1", reason="abandon", abandon=True)
        self.assertIsNone(self.tr.fleet()["leases"][0]["released_at"])

    def test_moved_branch_release_refused(self):
        path = self.make_task()
        self.dispatch()
        self.commit_in(path)
        (path / "g.txt").write_text("more\n")
        wt.commit_all(path, "unrecorded")
        with self.assertRaises(LeaseError):
            self.tr.release("t1", reason="abandon", abandon=True)
        self.assertEqual(wt.current_branch(path), "jbi/t1")

    def test_release_needs_reason(self):
        self.make_task()
        self.dispatch()
        with self.assertRaises(LeaseError):
            self.tr.release("t1", reason="because")
        with self.assertRaises(LeaseError):
            self.tr.release("t1", reason="merged")

    def test_release_without_lease_refused(self):
        self.make_task()
        with self.assertRaises(LeaseError):
            self.tr.release("t1", reason="x", abandon=True)


class Messages(Base):
    def test_queued_fanout(self):
        for name, sess in (("t1", "s1"), ("t2", "s2")):
            self.make_task(name, owns=(f"src/{name}",))
            self.dispatch(name, owns=(f"src/{name}",), session=sess)
        rows = self.tr.send("all", "hello")
        self.assertEqual(sorted(r["target"] for r in rows), ["s1", "s2"])
        self.assertTrue(all(r["delivery_state"] == "queued" for r in rows))
        rows = self.tr.send("s1", "one", recipients=["s2"])
        self.assertEqual(len(rows), 2)
        rows = self.tr.send("ignored", "x", all_sessions=True)
        self.assertEqual(len(rows), 2)
        with self.assertRaises(LeaseError):
            self.tr.send("all", "x", all_sessions=True)
        with self.assertRaises(LeaseError):
            self.tr.send("ghost", "x")
        self.assertEqual(len(self.tr.fleet()["messages"]), 6)


class Costs(Base):
    def test_null_is_unknown(self):
        self.make_task()
        did = self.dispatch()["dispatch_id"]
        self.tr.record_cost(did)
        row = self.tr.fleet()["costs"][0]
        self.assertTrue(all(row[k] is None for k in
                            ("input_tokens", "output_tokens", "cache_read", "cache_write", "cost_usd")))
        self.assertIsNone(self.tr.fleet()["cache_hit_rate"])
        self.tr.record_cost(did, cache_read=30, cache_write=10, cost_usd=0.5)
        self.tr.record_cost(did, input_tokens=5)
        fleet = self.tr.fleet()
        row = fleet["costs"][0]
        self.assertEqual((row["cache_read"], row["cache_write"], row["input_tokens"]), (30, 10, 5))
        self.assertEqual(fleet["cache_hit_rate"], 0.75)
        json.dumps(fleet)

    def test_zero_denominator_is_null(self):
        self.make_task()
        did = self.dispatch()["dispatch_id"]
        self.tr.record_cost(did, cache_read=0, cache_write=0)
        self.assertIsNone(self.tr.fleet()["cache_hit_rate"])

    def test_bad_counts_refused(self):
        self.make_task()
        did = self.dispatch()["dispatch_id"]
        for kw in ({"input_tokens": -1}, {"output_tokens": 1.5}, {"cache_read": True},
                   {"cache_write": "3"}, {"cost_usd": -0.1}, {"cost_usd": "1"}):
            with self.assertRaises(LeaseError, msg=str(kw)):
                self.tr.record_cost(did, **kw)
        with self.assertRaises(LeaseError):
            self.tr.record_cost(9999, input_tokens=1)


class Merge(Base):
    def test_green_without_evidence_cannot_merge(self):
        path = self.make_task()
        self.dispatch()
        head = self.commit_in(path)
        with self.assertRaises(LeaseError):
            self.tr.record_merge("t1", 7, head, head, "run-1")
        self.tr.record_ci("t1", pr_number=7, head_sha=head, base_commit=self.base,
                          service_reply={}, state="submitted")
        with self.assertRaises(LeaseError):
            self.tr.record_merge("t1", 7, head, head, "run-1")
        self.tr.record_ci("t1", pr_number=7, head_sha=head, base_commit=self.base,
                          service_reply={"ok": True}, green_run="run-1", state="green")
        for args in ((8, head, head, "run-1"), (7, self.base, head, "run-1"),
                     (7, head, head, "run-2"), (7, head, head, "")):
            with self.assertRaises(LeaseError, msg=str(args)):
                self.tr.record_merge("t1", *args)
        with self.assertRaises(LeaseError):
            self.tr.release("t1", reason="merged")
        self.assertEqual(self.tr.fleet()["merges"], [])
        self.assertNotEqual(self.tr.fleet()["tasks"][0]["state"], "merged")
        self.tr.record_merge("t1", 7, head, head, "run-1")
        self.assertEqual(self.tr.fleet()["tasks"][0]["state"], "merged")

    def test_bad_ci_input_refused(self):
        self.make_task()
        with self.assertRaises(LeaseError):
            self.tr.record_ci("t1", pr_number=0, head_sha=self.base, base_commit=self.base,
                              service_reply={})
        with self.assertRaises(LeaseError):
            self.tr.record_ci("t1", pr_number=1, head_sha="xyz", base_commit=self.base, service_reply={})


if __name__ == "__main__":
    unittest.main()
