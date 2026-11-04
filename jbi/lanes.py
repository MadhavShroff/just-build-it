"""CI lanes: the merge train.

Each lane is one worker thread and one tmux window that tails the lane log. A lane
takes one finished task at a time. It pins base = main, squash-merges the task branch
onto the base in a temporary worktree, runs the rigor level's lane checks, and merges
only when main is still the pinned base and the tested tree equals the merged tree.
When main moved, the lane rebuilds the combined tree and checks it again. A result is
cached by (base, head, checks), so the same inputs never run twice.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import rigor, worktrees as wt
from .agents import child_env
from .shim import Groups, run_command
from .store import Changes, Store

CHECK_TIMEOUT = 1800.0
MAX_REBUILDS = 5


@dataclass(frozen=True)
class MergeJob:
    task: str
    branch: str
    level: str
    worktree: Path


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    notes: list[str]


class LaneFailure(Exception):
    """The task cannot merge as it is. The findings go back to the implementer."""


class Lanes:
    def __init__(self, count: int, *, store: Store, repo: Path | Callable[[], Path], run_dir: Path, groups: Groups,
                 on_merged: Callable[[MergeJob, str], None], on_failed: Callable[[MergeJob, list[str]], None],
                 open_window: Callable[[str, Path], str | None] | None = None,
                 branch: str | Callable[[], str] = wt.MAIN, parent_pid: int | None = None):
        self.count, self.store, self.run_dir = count, store, run_dir
        self._branch = branch if callable(branch) else (lambda: branch)
        self.parent_pid = parent_pid or os.getpid()
        self._repo = repo if callable(repo) else (lambda: repo)   # read late: the target is set at bootstrap
        self.groups, self.on_merged, self.on_failed = groups, on_merged, on_failed
        self.open_window = open_window
        self.cache_path = run_dir / "lane-cache.json"
        self.check_runs = 0                      # checks actually executed, for tests and telemetry
        self._queue: "queue.Queue[MergeJob | None]" = queue.Queue()
        self._merge_lock = threading.Lock()      # one fast-forward of main at a time
        self._cache_lock = threading.Lock()
        self._busy = 0
        self._busy_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    # ---- lifecycle -------------------------------------------------------------------

    @property
    def repo(self) -> Path:
        return Path(self._repo())

    @property
    def branch(self) -> str:
        return self._branch()

    def start(self) -> None:
        for n in range(1, self.count + 1):
            log = self._log_path(n)
            log.parent.mkdir(parents=True, exist_ok=True)
            log.touch()
            window = self.open_window(f"lane-{n}", log) if self.open_window else None
            self.store.commit("lane.status", Changes().set("lanes", f"lane-{n}", {
                "status": "idle", "task": None, "base": None, "head": None, "window": window}))
            thread = threading.Thread(target=self._work, args=(n,), name=f"jbi-lane-{n}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def submit(self, job: MergeJob) -> None:
        self._queue.put(job)

    def idle(self) -> bool:
        with self._busy_lock:
            return self._busy == 0 and self._queue.empty()

    def close(self) -> None:
        for _ in self._threads:
            self._queue.put(None)

    # ---- the worker ------------------------------------------------------------------

    def _log_path(self, n: int) -> Path:
        return self.run_dir / "lanes" / f"lane-{n}.log"

    def _log(self, n: int, text: str) -> None:
        with self._log_path(n).open("a") as log:
            log.write(text.rstrip() + "\n")

    def _status(self, n: int, **values: object) -> None:
        changes = Changes()
        for key, value in values.items():
            changes.set("lanes", f"lane-{n}", key, value)
        self.store.commit("lane.status", changes, {"lane": f"lane-{n}"})

    def _work(self, n: int) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            with self._busy_lock:
                self._busy += 1
            try:
                self.merge(n, job)
            except Exception as error:          # a lane thread never dies
                self._safe_log(n, f"{job.task}: lane error {type(error).__name__}: {error}")
            finally:
                with self._busy_lock:
                    self._busy -= 1
                try:
                    self._status(n, status="idle", task=None)
                except Exception:
                    pass

    def _safe_log(self, n: int, text: str) -> None:
        try:
            self._log(n, text)
        except OSError:
            pass

    def merge(self, n: int, job: MergeJob) -> str | None:
        """Run one task through lane `n`. Returns the merged commit, or None on failure."""
        tmp = self.run_dir / "lanes" / f"lane-{n}-tree"
        try:
            sha = self._merge(n, job, tmp)
        except Exception as error:              # every error goes back to the task as a finding
            text = str(error) if isinstance(error, (LaneFailure, wt.GitError)) else f"{type(error).__name__}: {error}"
            self._safe_log(n, f"{job.task}: FAIL {text}")
            self.on_failed(job, [f"lane-{n}: {text}"])
            return None
        finally:
            try:
                wt.remove_worktree(self.repo, tmp)
            except Exception as error:
                self._safe_log(n, f"cannot remove {tmp}: {error}")
        self._log(n, f"{job.task}: merged {sha}")
        self.on_merged(job, sha)
        return sha

    def _merge(self, n: int, job: MergeJob, tmp: Path) -> str:
        checks = tuple(rigor.LEVELS[job.level]["lane_checks"])
        head = wt.head(self.repo, job.branch)
        if head is None:
            raise LaneFailure(f"branch {job.branch} does not exist")
        branch = self.branch
        for _ in range(MAX_REBUILDS):
            base = wt.head(self.repo, branch)
            if base is None:
                raise LaneFailure(f"base branch {branch} has no commit")
            self._status(n, status="testing", task=job.task, base=base, head=head)
            self._log(n, f"{job.task}: base {base[:10]} head {head[:10]} checks {list(checks) or 'none'}")
            tree = wt.squash_merge_tree(self.repo, base, job.branch, tmp)
            result = self.checked(base, head, checks, tmp, lane=n)
            if not result.ok:
                raise LaneFailure("; ".join(result.notes))
            with self._merge_lock:
                if wt.head(self.repo, branch) != base:
                    self._log(n, f"{job.task}: {branch} moved from {base[:10]}; rebuilding")
                    continue
                wt.git(tmp, "add", "-A")
                tested = wt.git(tmp, "write-tree")
                if tested != tree:
                    raise LaneFailure("the checks changed the tree; the tested tree is not the merged tree")
                self._status(n, status="merging")
                commit = wt.commit_tree(self.repo, tree, base, f"{job.task}: squash merge of {job.branch}")
                if wt.tree_of(self.repo, commit) != tree or not wt.fast_forward_main(self.repo, commit, base, branch):
                    raise LaneFailure(f"{branch} did not fast-forward to the verified commit")
                return commit
        raise LaneFailure(f"{branch} kept moving; gave up after {MAX_REBUILDS} rebuilds")

    # ---- checks and the cache --------------------------------------------------------

    def _cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return {}

    def checked(self, base: str, head: str, checks: tuple[str, ...], tree_dir: Path,
                lane: int = 1) -> CheckResult:
        """The check result for (base, head, checks): from the cache, or by running the checks."""
        key = f"{base}:{head}:{','.join(checks)}"
        with self._cache_lock:
            hit = self._cache().get(key)
        if hit is not None:
            self._log(lane, f"cache hit {key}")
            return CheckResult(hit["ok"], hit["notes"])
        result = self.run_checks(checks, tree_dir, lane)
        with self._cache_lock:
            cache = self._cache()
            cache[key] = {"ok": result.ok, "notes": result.notes}
            self.cache_path.write_text(json.dumps(cache, indent=1))
        return result

    def run_checks(self, checks: tuple[str, ...], tree_dir: Path, lane: int) -> CheckResult:
        if not checks:
            return CheckResult(True, ["no lane checks for this rigor level"])
        self.check_runs += 1
        try:
            commands = json.loads((tree_dir / ".jbi" / "commands.json").read_text())
        except (OSError, ValueError):
            commands = {}
        notes: list[str] = []
        for name in checks:
            line = commands.get(name)
            if not line:
                notes.append(f"{name}: skipped, no command in .jbi/commands.json")
                continue
            result = run_command(line, cwd=tree_dir, env=child_env(None), timeout=CHECK_TIMEOUT,
                                 parent_pid=self.parent_pid, groups=self.groups)
            code, out = result.returncode, result.output
            self._log(lane, f"$ {line}\n{out[-4000:]}")
            if code != 0:
                return CheckResult(False, [*notes, f"{name} failed ({'timeout' if code is None else code})",
                                           *out.strip().splitlines()[-10:]])
            notes.append(f"{name}: passed")
        return CheckResult(True, notes)
