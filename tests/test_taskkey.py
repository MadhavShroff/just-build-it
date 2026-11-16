"""Task key and work-cache proofs: REQUIREMENT-task-keys.md, tests 6.1 to 6.7.

Every test changes real state. 6.2 edits and commits a file on disk. 6.5 runs the
real supervisor and counts a real acceptance-command run. 6.6 removes the recorded
commit object from the repository and proves the task RE-RAN.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from jbi import protocol, rigor, taskkey, waves, worktrees as wt
from jbi.models import Choice
from jbi.store import Store
from jbi.supervisor import Supervisor

IMPLEMENT = rigor.stage("vibe", "implement")
MERGE = rigor.stage("vibe", "merge")
ACCEPTANCE = rigor.stage("medium", "acceptance")


def choice(model: str = "sim-s", provider: str = "simulator") -> Choice:
    return Choice(provider, model, "S")


class Repo:
    """A temp git repository with real commits."""

    def __init__(self, test: unittest.TestCase):
        self.path = Path(tempfile.mkdtemp(prefix="jbi-key-"))
        test.addCleanup(shutil.rmtree, self.path, True)
        wt.init_repo(self.path)

    def run_dir(self) -> Path:
        """ONE run directory OUTSIDE the repository, as a real run has it."""
        path = self.path.parent / f"{self.path.name}-run"
        path.mkdir(exist_ok=True)
        return path

    def commit(self, files: dict[str, str], message: str = "c") -> str:
        for name, text in files.items():
            path = self.path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            wt.git(self.path, "add", name)
        wt.git(self.path, "commit", "-q", "--no-verify", "-m", message)
        return wt.head(self.path)


def task(tid: str = "a", owns=(), deps=(), goal: str = "Build the thing",
         acceptance: str = "true") -> dict:
    return {"id": tid, "title": tid, "goal": goal, "deps": list(deps),
            "acceptance": acceptance, "owns": list(owns)}


class Hooks:
    """The runtime hooks a supervisor calls; the tests need the events only."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.merged_event, self.stopped_event = threading.Event(), threading.Event()

    def bootstrap_finished(self, ok, detail, commit): self.calls.append(("bootstrap", ok))
    def task_merged(self, task_, commit, patch): self.calls.append(("merged", task_))
    def wave_completed(self, wave): self.calls.append(("wave", wave))
    def all_merged(self): self.calls.append(("all",)); self.merged_event.set()
    def stopped(self, mode): self.calls.append(("stopped", mode)); self.stopped_event.set()


class Keys(unittest.TestCase):
    """6.1 to 6.4: what the key is, and what changes it."""

    def test_6_1_identical_inputs_share_one_key(self):
        """6.1: two tasks with identical inputs make one key, so the second reuses the first."""
        repo = Repo(self)
        base = repo.commit({"data/x.txt": "x\n"})
        first = task("a", owns=["data/x.txt"])
        second = task("b", owns=["data/x.txt"])
        one = taskkey.task_key(repo.path, first, base, choice(), level="vibe", stage=IMPLEMENT)
        two = taskkey.task_key(repo.path, second, base, choice(), level="vibe", stage=IMPLEMENT)
        self.assertIsNotNone(one)
        self.assertEqual(one, two, "identical inputs gave two keys (2.4)")

    def test_the_second_task_reads_the_first_result(self):
        """6.1, the cache half: the second task reads the first task's recorded result."""
        repo = Repo(self)
        base = repo.commit({"data/x.txt": "x\n"})
        worktree = repo.path                      # any checkout of `base`
        cache = taskkey.WorkCache(repo.run_dir(), "run-1")
        key = taskkey.task_key(repo.path, task("a", owns=["data/x.txt"]), base, choice(),
                               level="vibe", stage=IMPLEMENT)
        cache.record(key, IMPLEMENT, task="a", worktree=worktree, detail="done", model="sim-s")
        self.assertIsNone(taskkey.consult(cache, key, MERGE, repo=repo.path, worktree=worktree),
                          "a merge must never read the cache (4.2)")
        hit = taskkey.consult(taskkey.WorkCache(repo.run_dir(), "run-2"), key, IMPLEMENT,
                              repo=repo.path, worktree=worktree)
        self.assertIsNotNone(hit, "the second task did not reuse the first result")
        self.assertEqual((hit.stage, hit.run), ("implement", "run-1"))

    def test_6_2_a_changed_owned_file_changes_the_key(self):
        """6.2: a real edit and commit of an owned file changes the key."""
        repo = Repo(self)
        first = repo.commit({"data/x.txt": "one\n"})
        key_one = taskkey.task_key(repo.path, task("a", owns=["data/x.txt"]), first, choice(),
                                   level="vibe", stage=IMPLEMENT)
        (repo.path / "data/x.txt").write_text("two\n")          # change the file on disk
        second = repo.commit({"data/x.txt": "two\n"})
        key_two = taskkey.task_key(repo.path, task("a", owns=["data/x.txt"]), second, choice(),
                                   level="vibe", stage=IMPLEMENT)
        self.assertNotEqual(first, second)
        self.assertNotEqual(key_one, key_two, "a changed owned file kept the same key")

    def test_6_3_a_missing_path_differs_from_an_empty_file(self):
        """6.3: absent is a distinct input, never an empty string (2.3)."""
        repo = Repo(self)
        base = repo.commit({"data/empty.txt": ""})
        missing = taskkey.task_key(repo.path, task("a", owns=["data/nope.txt"]), base, choice(),
                                   level="vibe", stage=IMPLEMENT)
        empty = taskkey.task_key(repo.path, task("a", owns=["data/empty.txt"]), base, choice(),
                                 level="vibe", stage=IMPLEMENT)
        self.assertNotEqual(missing, empty, "a missing path read as an empty file")
        hashes = taskkey._hashes(repo.path, base, ["data/nope.txt", "data/empty.txt"])
        self.assertEqual(hashes["data/nope.txt"], taskkey.MISSING)
        self.assertNotEqual(hashes["data/empty.txt"], taskkey.MISSING)
        self.assertEqual(hashes["data/empty.txt"], wt.git(repo.path, "rev-parse", "main:data/empty.txt"))

    def test_6_4_reordering_owns_does_not_change_the_key(self):
        """6.4: the same inputs in any order give the same key (2.5)."""
        repo = Repo(self)
        base = repo.commit({"data/a.txt": "a\n", "data/b.txt": "b\n"})
        one = taskkey.task_key(repo.path, task("a", owns=["data/a.txt", "data/b.txt"]), base,
                               choice(), level="vibe", stage=IMPLEMENT)
        two = taskkey.task_key(repo.path, task("a", owns=["data/b.txt", "data/a.txt"]), base,
                               choice(), level="vibe", stage=IMPLEMENT)
        self.assertEqual(one, two, "the owns order changed the key")

    def test_normalise_leaves_no_rule_behind(self):
        """2.6: trim the ends, collapse whitespace runs, change no word and no case."""
        self.assertEqual(taskkey.normalise("  Build \n  The\tTHING  "), "Build The THING")

    def test_2_2_the_model_and_the_rigor_are_key_inputs(self):
        """2.2: a different provider, model or rigor level is a different input."""
        repo = Repo(self)
        base = repo.commit({"data/x.txt": "x\n"})
        one = taskkey.task_key(repo.path, task("a", owns=["data/x.txt"]), base,
                                      choice("sim-s"), level="vibe", stage=IMPLEMENT)
        other_model = taskkey.task_key(repo.path, task("a", owns=["data/x.txt"]), base,
                                       choice("sim-l"), level="vibe", stage=IMPLEMENT)
        other_level = taskkey.task_key(repo.path, task("a", owns=["data/x.txt"]), base,
                                       choice("sim-s"), level="high", stage=IMPLEMENT)
        self.assertNotEqual(one, other_model, "the model is not a key input")
        self.assertNotEqual(one, other_level, "the rigor level is not a key input")

    def test_a_dependency_key_changes_the_key(self):
        """2.2: the key of every dependency is a key input."""
        repo = Repo(self)
        base = repo.commit({"data/x.txt": "x\n"})
        dependant = task("a", owns=["data/x.txt"], deps=["p"])
        one = taskkey.task_key(repo.path, dependant, base, choice(), level="vibe",
                               stage=IMPLEMENT, dep_keys={"p": "1" * 64})
        two = taskkey.task_key(repo.path, dependant, base, choice(), level="vibe",
                               stage=IMPLEMENT, dep_keys={"p": "2" * 64})
        self.assertNotEqual(one, two)

    def test_only_work_is_cacheable(self):
        """4.1, 4.2, 5.1 and 6.5: a hit may skip the work and no check."""
        self.assertTrue(taskkey.cacheable(IMPLEMENT))
        for stage in (ACCEPTANCE, MERGE,
                      rigor.stage("high", "verify"), rigor.stage("high", "security"),
                      *rigor.review_stages()):
            self.assertFalse(taskkey.cacheable(stage), f"{stage.name} must always run")


class CacheSafety(unittest.TestCase):
    """3.1, 4.2 and 5.3: a key the cache cannot see is CHANGED, and the cache is disposable."""

    def test_3_1_an_unknown_input_gives_no_key(self):
        """3.1: an unreadable base and a dependency with no key both give None, never a key."""
        repo = Repo(self)
        base = repo.commit({"data/x.txt": "x\n"})
        unknown_base = taskkey.task_key(repo.path, task("a"), "0" * 40, choice(),
                                        level="vibe", stage=IMPLEMENT)
        self.assertIsNone(unknown_base, "an unreadable base still gave a key")
        no_dep_key = taskkey.task_key(repo.path, task("a", deps=["p"]), base, choice(),
                                      level="vibe", stage=IMPLEMENT, dep_keys={})
        self.assertIsNone(no_dep_key, "a dependency with no key still gave a key")
        with_dep = taskkey.task_key(repo.path, task("a", deps=["p"]), base, choice(),
                                    level="vibe", stage=IMPLEMENT, dep_keys={"p": "deadbeef"})
        self.assertIsNotNone(with_dep)

    def test_a_none_key_never_hits_even_with_a_full_cache(self):
        """3.1 and 4.2: a None key and a gate role both miss, whatever the file holds."""
        repo = Repo(self)
        base = repo.commit({"data/x.txt": "x\n"})
        cache = taskkey.WorkCache(repo.run_dir(), "run-1")
        key = taskkey.task_key(repo.path, task("a", owns=["data/x.txt"]), base, choice(),
                              level="vibe", stage=IMPLEMENT)
        cache.record(key, IMPLEMENT, task="a", worktree=repo.path, detail="d", model=None)
        self.assertIsNone(taskkey.consult(cache, None, IMPLEMENT, repo=repo.path,
                                          worktree=repo.path))
        self.assertIsNone(taskkey.consult(cache, key, ACCEPTANCE, repo=repo.path,
                                          worktree=repo.path), "a gate role read the cache")
        self.assertIsNone(taskkey.consult(cache, key, MERGE, repo=repo.path, worktree=repo.path))

    def test_a_result_whose_commit_is_gone_does_not_hit(self):
        """3.1: the recorded work is an input too. A removed commit makes the result unknown."""
        repo = Repo(self)
        base = repo.commit({"data/x.txt": "x\n"})
        # A detached worktree makes a commit that NO branch holds, so git can prune it.
        tree = repo.path.parent / f"{repo.path.name}-wt"
        self.addCleanup(shutil.rmtree, tree, True)
        wt.git(repo.path, "worktree", "add", "-q", "--detach", str(tree), base)
        (tree / "data" / "x.txt").write_text("the work\n")
        wt.git(tree, "commit", "-q", "-a", "--no-verify", "-m", "work")
        cache = taskkey.WorkCache(repo.run_dir(), "run-1")
        key = taskkey.task_key(repo.path, task("a", owns=["data/x.txt"]), base, choice(),
                               level="vibe", stage=IMPLEMENT)
        cache.record(key, IMPLEMENT, task="a", worktree=tree, detail="d", model=None)
        self.assertIsNotNone(cache.before(key, IMPLEMENT, repo=repo.path, worktree=tree),
                             "a live commit did not hit")
        wt.git(repo.path, "worktree", "remove", "--force", str(tree))
        wt.git(repo.path, "reflog", "expire", "--expire=now", "--all", check=False)
        wt.git(repo.path, "gc", "-q", "--prune=now", check=False)
        entry = json.loads(cache.path.read_text())[key]
        self.assertNotEqual(wt.git(repo.path, "cat-file", "-t", entry["commit"], check=False),
                            "commit", "the test did not remove the commit")
        self.assertIsNone(cache.before(key, IMPLEMENT, repo=repo.path, worktree=repo.path),
                          "a vanished commit still read as a hit")
    def test_5_3_a_broken_cache_file_is_a_miss(self):
        """5.3: a cache that cannot be read makes the next run correct, only slower."""
        repo = Repo(self)
        cache = taskkey.WorkCache(repo.run_dir(), "run-1")
        cache.path.parent.mkdir(parents=True, exist_ok=True)
        cache.path.write_text("{not json")
        self.assertEqual(cache.read(), {})
        self.assertIsNone(cache.lookup("anything"))


class Run(unittest.TestCase):
    """A real supervisor, a real repository, the simulator agent, no tmux.

    Each `supervisor()` call is one RUN: its own run directory and its own store. A
    second run over the same repository shares the work cache by a copied file.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jbi-keyrun-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        wt.init_repo(self.repo)
        (self.repo / "README").write_text("base\n")
        wt.commit_all(self.repo, "base")
        self.runs = 0

    def supervisor(self, tasks: list[dict], rigor_name: str = "vibe", share: Path | None = None):
        """One run: its own run directory. `share` copies a work cache in from another run."""
        self.runs += 1
        run_dir = self.tmp / f"run-{self.runs}"
        run_dir.mkdir(parents=True)
        if share is not None and (share / taskkey.CACHE_NAME).exists():
            shutil.copy(share / taskkey.CACHE_NAME, run_dir / taskkey.CACHE_NAME)
        state = protocol.initial_state(f"t-{uuid.uuid4().hex[:8]}", "now", name="t",
                                       mode="new", target=str(self.repo), max_sessions=1,
                                       lanes=1, tmux="")
        state["config"]["rigor"] = rigor_name
        state["plan"] = waves.compile_plan(tasks, 1)
        store = Store(run_dir / "run.db", state)
        self.addCleanup(store.close)
        hooks = Hooks()
        sup = Supervisor(store, hooks, run_dir, self.repo, use_tmux=False)
        self.addCleanup(lambda: (sup.stop("kill"), hooks.stopped_event.wait(20)))
        return sup, store, hooks

    def merged(self, sup, hooks, store, tid: str = "a", timeout: float = 90) -> None:
        sup.start_execution()
        self.assertTrue(hooks.merged_event.wait(timeout), store.read("/timeline"))
        self.assertEqual(store.read(f"/plan/tasks/{tid}/status"), "merged")

    def drop_worktree(self, run_dir: Path, tid: str = "a") -> None:
        wt.remove_worktree(self.repo, run_dir / "worktrees" / tid)
        wt.git(self.repo, "branch", "-D", f"jbi/{tid}", check=False)


class HitStillGates(Run):
    """6.5: a cache hit skips work and never a gate."""

    def test_6_5_a_hit_still_runs_the_acceptance_command_and_the_merge(self):
        log = self.tmp / "accept.log"
        acceptance = f"echo ran >> {log}; true"
        sup, store, hooks = self.supervisor([task("a", acceptance=acceptance)], rigor_name="medium")
        self.merged(sup, hooks, store)
        self.assertEqual(log.read_text().count("ran"), 1, "the first run skipped acceptance")
        first_sha = wt.head(self.repo, "main")
        self.drop_worktree(self.tmp / "run-1")

        warm = self.tmp / "run-1"
        sup2, store2, hooks2 = self.supervisor([task("b", acceptance=acceptance)], rigor_name="medium",
                                               share=warm)
        self.merged(sup2, hooks2, store2, "b")
        # The implement stage read the cache: no agent session ran for it.
        self.assertIsNone(store2.read("/sessions/s-b-implement-1/status"),
                          "a hit still started an implement session")
        hits = [row["text"] for row in store2.read("/timeline") or []
                if row.get("kind") == "task" and "cache hit" in str(row.get("text"))]
        self.assertTrue(hits, "the hit was not recorded (4.3)")
        # 4.2 and 6.5: the acceptance command ran a second time, and the merge landed.
        self.assertEqual(log.read_text().count("ran"), 2,
                         "a cache hit skipped the acceptance command")
        self.assertEqual(store2.read("/plan/tasks/b/stages")["acceptance"]["status"], "passed")
        self.assertEqual(store2.read("/plan/tasks/b/stages")["merge"]["status"], "passed")
        self.assertNotEqual(wt.head(self.repo, "main"), first_sha, "a cache hit skipped the merge")


class UnknownInputReruns(Run):
    """6.6: an input the key cannot see makes the task RE-RUN."""

    def test_6_6_a_gone_recorded_commit_makes_the_task_rerun(self):
        sup, store, hooks = self.supervisor([task("a")], rigor_name="vibe")
        self.merged(sup, hooks, store)
        warm = self.tmp / "run-1"
        entry = json.loads((warm / taskkey.CACHE_NAME).read_text())
        self.assertTrue(entry, "the first run recorded nothing")
        implement_commit = next(iter(entry.values()))["commit"]
        self.drop_worktree(warm, "a")
        wt.git(self.repo, "reflog", "expire", "--expire=now", "--all", check=False)
        wt.git(self.repo, "gc", "-q", "--prune=now", check=False)
        self.assertNotEqual(wt.git(self.repo, "cat-file", "-t", implement_commit, check=False),
                            "commit", "the test did not remove the recorded commit")

        sup2, store2, hooks2 = self.supervisor([task("b")], rigor_name="vibe", share=warm)
        self.merged(sup2, hooks2, store2, "b")
        # A new implement session exists, so the task RE-RAN and did not hit (3.1).
        self.assertIsNotNone(store2.read("/sessions/s-b-implement-1/status"),
                             "an unknown input did not re-run the implement stage")
        hits = [row for row in store2.read("/timeline") or []
                if row.get("kind") == "task" and "cache hit" in str(row.get("text"))]
        self.assertEqual(hits, [], "an unknown input reported a cache hit")


class DeletedCache(Run):
    """6.7: deleting the cache gives the same final result."""

    def test_6_7_deleting_the_cache_keeps_the_final_result(self):
        sup, store, hooks = self.supervisor([task("a")], rigor_name="vibe")
        self.merged(sup, hooks, store)
        warm_tree = wt.git(self.repo, "rev-parse", "main^{tree}")
        self.drop_worktree(self.tmp / "run-1", "a")

        sup2, store2, hooks2 = self.supervisor([task("a")], rigor_name="vibe")
        self.merged(sup2, hooks2, store2)
        self.assertEqual(wt.git(self.repo, "rev-parse", "main^{tree}"), warm_tree,
                         "a cold run changed the final result (5.3)")
        self.assertEqual(store2.read("/plan/tasks/a/stages")["merge"]["status"], "passed")
        # The cold run is the control: it proves the cache is what made run 2 fast, and
        # that removing the cache changes no result (5.3).


if __name__ == "__main__":
    unittest.main()
