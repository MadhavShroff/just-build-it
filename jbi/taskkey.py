"""Content-addressed task keys and the work cache.

The rule is REQUIREMENT-task-keys.md. Every behaviour cites its number.

A task key hashes the task's INPUTS, never its ID (2.1). The cache in the run
directory maps one key to the result of one stage (4.1). A cache hit skips WORK and
never a gate (4.2, 6.5): the merge is always attempted and the acceptance command
always runs.

The shape matches `jbi.lanes`, which caches a CI result by (base, head, checks) with
the same intent: one flat JSON file in the run directory, read under a lock and
written whole. The two cannot share code. A lane key covers immutable commit ids; a
task key covers task text plus file content at a moving base, and it needs a
per-stage model choice. So the key builders stay separate, and the file discipline
is shared by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import __version__, models, rigor, worktrees as wt

# 3.3: a key input changed, so no result is reused across this boundary.
KEY_VERSION = 1

# 2.3: a path absent at the base commit is a distinct input, never an empty string.
MISSING = "missing"

# 3.1: a stage whose result turns on an input the key does not hash reads as CHANGED,
# so it re-runs. Only `implement` has a key that covers all of its inputs: `owns`
# content, the goal, the acceptance text and the model. A verify, a review, a security
# scan or an audit reads the whole diff against a moving base, which this key does not
# hash. So the cache may skip the work and no check.
CACHEABLE_STAGES = ("implement",)

# 4.2 and 6.5: a merge is a gate and the acceptance command is a gate.
NEVER_CACHED_ROLES = ("acceptance", "merge")

# 3.2: no environment variable, a clock or a random source is folded into the key.
CACHE_NAME = "task-cache.json"


@dataclass(frozen=True)
class Hit:
    """One stage that reads a recorded result instead of running (4.1, 4.3)."""
    key: str
    stage: str
    run: str
    commit: str | None
    detail: str
    model: str | None


def cacheable(stage) -> bool:
    """True when a cache hit MAY skip this stage (4.1).

    This is the intersection of 4.1's permission and the rules that forbid a skip:
    4.2 and 6.5 keep the merge and the acceptance command. 3.1 keeps every stage
    whose key cannot see all of its inputs, and 5.1 and 5.2 keep every check.
    """
    return stage.name in CACHEABLE_STAGES and stage.role not in NEVER_CACHED_ROLES


def normalise(text: str) -> str:
    """2.6: strip the ends, collapse whitespace runs to one space, change no word."""
    return " ".join(str(text or "").split())


def _owned(raw: str) -> str:
    text = str(raw or "").strip()
    return (text[2:] if text.startswith("./") else text).rstrip("/")


def _hashes(repo: Path, base: str, paths: list[str]) -> dict[str, str] | None:
    """The git content id for each owned path at `base` (2.2, 2.3). None when unreadable."""
    if not _is_commit(repo, base):
        return None                                   # 3.1: the base itself is unknown
    if not paths:
        return {}
    try:
        out = wt.git(repo, "ls-tree", "-z", base, "--", *[f":(literal){p}" for p in paths])
    except wt.GitError:
        return None                                   # 3.1: an input the key cannot see
    found: dict[str, str] = {}
    for entry in out.split("\0"):
        if not entry or "\t" not in entry:
            continue
        meta, name = entry.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3:
            found[name] = parts[2]
    return {p: found.get(p, MISSING) for p in paths}


def task_key(repo: Path | str, task: dict, base: str, choice, *, level: str, stage,
             dep_keys: dict[str, str | None] | None = None,
             findings: list[str] | None = None) -> str | None:
    """The key of one stage of one task from its inputs (2.1, 2.2).

    `choice` is the resolved stage choice: an object with `provider`, `model` and
    `tier`. `dep_keys` maps each dependency id to its own key for the same stage.

    Returns None when an input is unknown. An unknown input means CHANGED (3.1), so a
    caller must treat None as a miss and as a reason to re-run, never as a key.

    The base commit is an argument because it scopes the file content (2.2). The base
    commit id is not itself a field: a merge moves the base without changing an owned
    file, and 3.2 forbids folding an input in to make a hit more likely.

    `findings` is an input too. It is the text a failed attempt returned, so a retry
    that carries new findings has a new key and re-runs. That keeps 3.1 true for the
    one input a retry adds.
    """
    paths = sorted({p for p in (_owned(o) for o in (task.get("owns") or ())) if p})
    hashes = _hashes(Path(repo), base, paths)
    if hashes is None:
        return None
    pairs: list[list[str]] = []
    for tid in sorted(task.get("deps") or ()):
        key = (dep_keys or {}).get(tid)
        if not key:
            return None                               # 3.1: a dependency without a key is unknown
        pairs.append([tid, key])
    payload = {
        "v": KEY_VERSION,
        "jbi": __version__,
        "goal": normalise(task.get("goal")),
        "owns": sorted([path, sha] for path, sha in hashes.items()),
        "deps": pairs,
        "acceptance": normalise(task.get("acceptance")),
        "retry": normalise(" ".join(findings or ())),
        "rigor": level,
        "stage": stage.name,
        "role": stage.role,
        "provider": getattr(choice, "provider", None),
        "model": getattr(choice, "model", None),
        "tier": getattr(choice, "tier", None),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


class WorkCache:
    """One small JSON file in the run directory, keyed by task key (4.1, 4.3, 5.3).

    One entry per stage key. That makes a partial hit stage-level, never
    task-level-only (4.4): a later stage hits only when its own key repeats.
    """

    def __init__(self, run_dir: Path | str, run_id: str):
        self.path = Path(run_dir) / CACHE_NAME
        self.run_id = str(run_id or "run")
        self._lock = threading.Lock()

    # ---- reading and writing ---------------------------------------------------------

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def lookup(self, key: str | None) -> dict | None:
        """The recorded entry for `key`, or None. A None key is always a miss (3.1)."""
        if not key:
            return None
        with self._lock:
            entry = self.read().get(key)
        return entry if isinstance(entry, dict) else None

    def _write(self, cache: dict) -> None:
        """Write the whole file at once, so a crash leaves the old file, never half of one."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".task-cache-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as out:
                json.dump(cache, out, indent=1, sort_keys=True)
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    # ---- the key for one task and stage ----------------------------------------------

    def key_for(self, repo: Path | str, tasks: dict, tid: str, stage, *, base: str,
                state: dict, findings: list[str] | None = None,
                _memo: dict | None = None) -> str | None:
        """The key of `stage` for task `tid`, with each dependency's key of that stage."""
        memo = {} if _memo is None else _memo
        if tid in memo:
            return memo[tid]
        task = tasks.get(tid)
        if not isinstance(task, dict):
            return None
        memo[tid] = None                     # a cycle reads as unknown, never as a key
        level, _profile = rigor.effective(task, state.get("config") or {})
        choice = models.select(state, stage.role, task=task, attempts=task.get("attempts", 0))
        dep_keys = {dep: self.key_for(repo, tasks, dep, stage, base=base, state=state, _memo=memo)
                    for dep in (task.get("deps") or ())}
        memo[tid] = task_key(repo, task, base, choice, level=level, stage=stage,
                             dep_keys=dep_keys, findings=findings)
        return memo[tid]

    # ---- consulting and recording ----------------------------------------------------

    def before(self, key: str | None, stage, *, repo: Path | str, worktree: Path) -> Hit | None:
        """A usable recorded result for `key`, or None (3.1, 4.1, 4.2).

        An implement hit puts the work back in the worktree, so every later stage runs
        against the real code: the acceptance command, the reviews and the merge.
        """
        entry = self.lookup(key)
        if entry is None or entry.get("stage") != stage.name:
            return None
        commit = entry.get("commit")
        if not _is_commit(repo, commit):
            return None                      # 3.1: the recorded work is gone, so it is unknown
        if stage.name == "implement":
            if wt.dirty_paths(worktree):
                # 3.1: the worktree holds uncommitted work. Its content is an input the
                # key does not hash, so it reads as CHANGED. A hit never discards it.
                return None
            # A detached checkout moves no branch. A branch checkout moves branch
            # jbi/<task>, so the merge stage lands exactly this work.
            wt.git(worktree, "reset", "-q", "--hard", str(commit))
            # 4.3: the record names the key and the run the result came from.
            detail = (f"cache hit key {str(key)[:12]} run {entry.get('run')}: "
                      f"implement reused {str(commit)[:10]}")
        else:
            # A later stage is a hit only against the exact tree it passed on.
            if wt.head(worktree, "HEAD") != commit:
                return None
            detail = f"cache hit key {str(key)[:12]} run {entry.get('run')}: {entry.get('detail') or ''}"
        self._bump(key)
        return Hit(str(key), stage.name, str(entry.get("run")), commit, detail, entry.get("model"))

    def record(self, key: str | None, stage, *, task: str, worktree: Path,
               detail: str, model: str | None) -> None:
        """Store one passed stage under `key` (4.1, 4.3). A None key records nothing (3.1)."""
        commit = wt.head(worktree, "HEAD")
        if not key or not commit:
            return
        with self._lock:
            cache = self.read()
            cache[key] = {"task": task, "stage": stage.name, "run": self.run_id, "commit": commit,
                          "detail": str(detail)[:2000], "model": model}
            self._write(cache)

    def _bump(self, key: str | None) -> None:
        if not key:
            return
        with self._lock:
            cache = self.read()
            entry = cache.get(key)
            if isinstance(entry, dict):
                entry["hits"] = int(entry.get("hits", 0)) + 1
                cache[key] = entry
                self._write(cache)


def _is_commit(repo: Path | str, sha: object) -> bool:
    """True when `sha` names a commit object in `repo` (3.1)."""
    if not isinstance(sha, str) or not sha:
        return False
    return wt.git(repo, "cat-file", "-t", sha, check=False) == "commit"


def consult(cache: WorkCache, key: str | None, stage, *, repo: Path | str,
            worktree: Path) -> Hit | None:
    """The recorded result this stage may read, or None (4.1, 4.2).

    A gate role always returns None: the acceptance command and the merge run every
    time (4.2, 6.5).
    """
    if not cacheable(stage):
        return None
    return cache.before(key, stage, repo=repo, worktree=worktree)


def remember(cache: WorkCache, key: str | None, stage, *, task: str, worktree: Path,
             ok: bool, detail: str, model: str | None) -> None:
    """Record a PASSED stage under its key (4.1, 4.3). A gate role is never recorded (4.2)."""
    if not ok or not cacheable(stage):
        return
    cache.record(key, stage, task=task, worktree=worktree, detail=detail, model=model)


__all__ = ["Hit", "WorkCache", "cacheable", "consult", "normalise", "remember", "task_key"]
