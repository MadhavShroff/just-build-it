"""Internal tracking store: tasks, sessions, dispatches, leases, CI records, merges, messages, costs.

The store is one SQLite file. It works offline and uses no other state store.
Every write runs in one BEGIN IMMEDIATE transaction. A lease is the only write grant.
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import posixpath
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .worktrees import GitError, git, pin_git_metadata, trusted_git, unpin_git_metadata

ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
GLOB_CHARS = "*?["
CI_STATES = ("submitted", "green", "failed")
TASK_STATES = ("created", "dispatched", "running", "verifying", "queued", "merging", "merged",
               "failed", "paused", "blocked", "abandoned")
SESSION_STATUSES = ("idle", "busy", "reset", "failed", "ended")
DELIVERY_STATES = ("queued", "sent", "delivered", "failed", "read")
HISTORY_ROLES = ("user", "assistant", "system")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL, owns TEXT NOT NULL,
  branch TEXT NOT NULL, worktree TEXT NOT NULL, base_commit TEXT NOT NULL,
  head_commit TEXT, state TEXT NOT NULL, node TEXT, release_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, node TEXT, history TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reset_at TEXT);
CREATE TABLE IF NOT EXISTS dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id), session_id TEXT NOT NULL REFERENCES sessions(id),
  attempt INTEGER NOT NULL, model TEXT, warm INTEGER NOT NULL, brief_digest TEXT,
  started_at TEXT NOT NULL, finished_at TEXT, receipt TEXT, verdict TEXT);
CREATE TABLE IF NOT EXISTS leases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id), session_id TEXT NOT NULL REFERENCES sessions(id),
  owns TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL, base_commit TEXT NOT NULL,
  granted_at TEXT NOT NULL, released_at TEXT, release_reason TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_lease ON leases(task_id) WHERE released_at IS NULL;
CREATE TABLE IF NOT EXISTS ci_submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id), pr_number INTEGER NOT NULL, head_sha TEXT NOT NULL,
  base_commit TEXT NOT NULL, service_reply TEXT NOT NULL, green_run TEXT, squash_sha TEXT,
  state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS merges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id), pr_number INTEGER NOT NULL,
  head_sha TEXT NOT NULL, squash_sha TEXT NOT NULL, green_run TEXT NOT NULL, merged_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender TEXT NOT NULL, target TEXT NOT NULL REFERENCES sessions(id), text TEXT NOT NULL,
  created_at TEXT NOT NULL, delivery_state TEXT NOT NULL, receipt TEXT);
CREATE TABLE IF NOT EXISTS costs (
  dispatch_id INTEGER PRIMARY KEY REFERENCES dispatches(id),
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
  cost_usd REAL, updated_at TEXT NOT NULL);
"""


class LeaseError(RuntimeError):
    """A lease rule refused the request. The message names the actors."""


class InvalidInput(LeaseError, ValueError):
    """An argument has a bad format. The store changes nothing."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _check_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise InvalidInput(f"bad {what} id: {value!r}")
    return value


def _check_sha(value: Any, what: str) -> str:
    if not isinstance(value, str) or not SHA_RE.match(value):
        raise InvalidInput(f"bad {what} commit id: {value!r}")
    return value


def _check_sha40(value: Any, what: str) -> str:
    if not isinstance(value, str) or not SHA40_RE.match(value):
        raise InvalidInput(f"bad {what} commit id: {value!r}. A full 40 character id is required")
    return value


def normalize_path(raw: Any) -> str:
    """Return the canonical repo-relative posix path. The file system is never read."""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidInput(f"empty path: {raw!r}")
    if "\x00" in raw or "\\" in raw:
        raise InvalidInput(f"bad character in path: {raw!r}")
    if raw.startswith("/"):
        raise InvalidInput(f"absolute path refused: {raw!r}")
    if any(part == ".." for part in raw.split("/")):
        raise InvalidInput(f"parent segment refused: {raw!r}")
    clean = posixpath.normpath(raw)
    if clean in (".", "") or clean.startswith("/") or clean == ".." or clean.startswith("../"):
        raise InvalidInput(f"path names no entry: {raw!r}")
    return clean


def normalize_owns(owns: Iterable[str]) -> list[str]:
    """Return the sorted, unique, canonical owns list. An empty list is refused."""
    if isinstance(owns, (str, bytes)):
        raise InvalidInput("owns must be a list of paths")
    items = sorted({normalize_path(item) for item in owns})
    if not items:
        raise InvalidInput("owns is empty")
    return items


def _prefix(pattern: str) -> list[str]:
    out: list[str] = []
    for part in pattern.split("/"):
        if any(ch in part for ch in GLOB_CHARS):
            break
        out.append(part)
    return out


def paths_overlap(a: str, b: str) -> bool:
    """Return True when two owns entries can touch one path. The test is conservative."""
    pa, pb = _prefix(a), _prefix(b)
    short = min(len(pa), len(pb))
    return pa[:short] == pb[:short]


def path_matches(path: str, owns: Iterable[str]) -> bool:
    """Return True when `path` equals an entry, sits under it, or matches its glob."""
    for pattern in owns:
        if path == pattern or path.startswith(pattern + "/"):
            return True
        if any(ch in pattern for ch in GLOB_CHARS) and fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _count(value: Any, what: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidInput(f"{what} must be a nonnegative integer or None")
    return value


class Tracking:
    """One SQLite store for fleet records. A lease grants every tool write."""

    def __init__(self, path: Path):
        self.path = Path(path)
        parent = self.path.parent
        if not parent.exists():
            parent.mkdir(mode=0o700, parents=True)
            os.chmod(parent, 0o700)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(self.path, 0o600)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(dispatches)")}
        if "continuation" not in columns:
            if "warm" not in columns:
                raise LeaseError("dispatches table has no mode column")
            self._db.execute("ALTER TABLE dispatches RENAME COLUMN warm TO continuation")
        for suffix in ("-wal", "-shm"):
            side = Path(str(self.path) + suffix)
            if side.exists():
                os.chmod(side, 0o600)

    # ---- helpers -------------------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    @staticmethod
    def _task_dict(row: sqlite3.Row | dict) -> dict:
        out = dict(row)
        out["owns"] = json.loads(out["owns"])
        return out

    def _task(self, task_id: str, db: sqlite3.Connection | None = None) -> dict:
        _check_id(task_id, "task")
        row = (db or self._db).execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise LeaseError(f"task {task_id} is unknown")
        return self._task_dict(row)

    def _live_lease(self, task_id: str, db: sqlite3.Connection | None = None) -> dict | None:
        row = (db or self._db).execute(
            "SELECT * FROM leases WHERE task_id=? AND released_at IS NULL", (task_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["owns"] = json.loads(out["owns"])
        return out

    def get_task(self, task_id: str) -> dict | None:
        """Read one task without loading the fleet. Unknown tasks return None."""
        _check_id(task_id, "task")
        row = self._one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return self._task_dict(row) if row is not None else None

    def start_history_session(self, session_id: str, node: str | None = None) -> dict:
        """Create one task-stage history endpoint. Existing history never moves."""
        _check_id(session_id, "session")
        stamp = _now()
        with self._tx() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO sessions (id,node,history,status,created_at,updated_at) "
                           "VALUES (?,?,'[]','idle',?,?)", (session_id, node, stamp, stamp))
                row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            elif node is not None and row["node"] is not None and row["node"] != node:
                raise LeaseError(f"history session {session_id} belongs to node {row['node']}, not {node}")
            return dict(row)

    def table_names(self) -> list[str]:
        """Return the sorted table names of the store."""
        rows = self._all("SELECT name FROM sqlite_master WHERE type='table' "
                         "AND name NOT LIKE 'sqlite_%' ORDER BY name")
        return [r["name"] for r in rows]

    # ---- tasks and dispatch ----------------------------------------------------------

    def create_task(self, *, title: str, goal: str = "", owns: Iterable[str], branch: str,
                    worktree: Path, base_commit: str, task_id: str | None = None,
                    node: str | None = None) -> dict:
        """Insert one task in state created. Return the task record."""
        task_id = _check_id(task_id, "task") if task_id is not None else "t" + uuid.uuid4().hex[:12]
        if not isinstance(title, str) or not title.strip():
            raise InvalidInput("title is empty")
        if not isinstance(branch, str) or not branch.strip():
            raise InvalidInput("branch is empty")
        _check_sha(base_commit, "base")
        owned = normalize_owns(owns)
        stamp = _now()
        with self._tx() as db:
            if db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise LeaseError(f"task {task_id} exists")
            db.execute("INSERT INTO tasks (id,title,goal,owns,branch,worktree,base_commit,state,node,"
                       "created_at,updated_at) VALUES (?,?,?,?,?,?,?,'created',?,?,?)",
                       (task_id, title, goal, json.dumps(owned), branch, str(worktree), base_commit,
                        node, stamp, stamp))
            return self._task(task_id, db)

    def dispatch(self, task_id: str, *, session_id: str, owns: Iterable[str], branch: str,
                 worktree: Path, model: str | None = None, continuation: bool = False,
                 brief_digest: str | None = None) -> dict:
        """Insert the session, the dispatch, and the lease in one transaction."""
        _check_id(task_id, "task")
        _check_id(session_id, "session")
        owned = normalize_owns(owns)
        if not isinstance(continuation, bool):
            raise InvalidInput("continuation must be true or false")
        stamp = _now()
        with self._tx() as db:
            task = self._task(task_id, db)
            if self._live_lease(task_id, db):
                raise LeaseError(f"task {task_id} holds a live lease. Release it before redispatch")
            if task["state"] not in ("created", "abandoned"):
                raise LeaseError(f"task {task_id} is {task['state']} and cannot dispatch")
            if owned != task["owns"]:
                raise LeaseError(f"task {task_id} owns differ from the task record")
            if branch != task["branch"]:
                raise LeaseError(f"task {task_id} branch {branch!r} differs from {task['branch']!r}")
            if str(worktree) != task["worktree"]:
                raise LeaseError(f"task {task_id} worktree {str(worktree)!r} differs from the task record")
            for held in db.execute("SELECT * FROM leases WHERE released_at IS NULL").fetchall():
                for new in owned:
                    for old in json.loads(held["owns"]):
                        if paths_overlap(new, old):
                            raise LeaseError(
                                f"lease conflict: task {task_id} session {session_id} asks for {new!r}, "
                                f"which overlaps {old!r} held by task {held['task_id']} "
                                f"session {held['session_id']}")
            db.execute("INSERT INTO sessions (id,node,status,created_at,updated_at) VALUES (?,?,'busy',?,?) "
                       "ON CONFLICT(id) DO UPDATE SET status='busy', updated_at=excluded.updated_at",
                       (session_id, task["node"], stamp, stamp))
            attempt = db.execute("SELECT COUNT(*) FROM dispatches WHERE task_id=?", (task_id,)).fetchone()[0] + 1
            cur = db.execute("INSERT INTO dispatches (task_id,session_id,attempt,model,continuation,brief_digest,"
                             "started_at) VALUES (?,?,?,?,?,?,?)",
                             (task_id, session_id, attempt, model, 1 if continuation else 0, brief_digest, stamp))
            dispatch_id = cur.lastrowid
            db.execute("INSERT INTO leases (task_id,session_id,owns,branch,worktree,base_commit,granted_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (task_id, session_id, json.dumps(owned), task["branch"], task["worktree"],
                        task["base_commit"], stamp))
            db.execute("UPDATE tasks SET state='dispatched', updated_at=? WHERE id=?", (stamp, task_id))
            return {"dispatch_id": dispatch_id,
                    "task": self._task(task_id, db),
                    "session": dict(db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()),
                    "dispatch": dict(db.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()),
                    "lease": self._live_lease(task_id, db)}

    def authorize_write(self, task_id: str, path: str, *, tool_write: bool = True) -> None:
        """Check one write. Return the canonical path, or None for the scratch and receipt route."""
        _check_id(task_id, "task")
        clean = normalize_path(path)
        with self._lock:
            row = self._one("SELECT id FROM tasks WHERE id=?", (task_id,))
            lease = self._live_lease(task_id) if row else None
        if lease is None:
            raise LeaseError(f"no live lease for task {task_id}: write to {clean!r} refused")
        if lease["task_id"] != task_id:
            raise LeaseError(f"lease identity mismatch for task {task_id}")
        if not tool_write:
            return None
        if not path_matches(clean, lease["owns"]):
            raise LeaseError(f"task {task_id} session {lease['session_id']} does not own {clean!r}")
        return clean

    def record_commit(self, task_id: str, sha: str) -> dict:
        """Record the commit that the task branch must point at when release runs."""
        _check_sha(sha, "task")
        with self._tx() as db:
            self._task(task_id, db)
            db.execute("UPDATE tasks SET head_commit=?, updated_at=? WHERE id=?", (sha, _now(), task_id))
            return self._task(task_id, db)

    # ---- release -----------------------------------------------------------------------

    def release(self, task_id: str, *, reason: str, abandon: bool = False, repository: Path | None = None) -> dict:
        """Park the branch, detach the worktree at the base, then revoke the lease."""
        task = self._task(task_id)
        if self._live_lease(task_id) is None:
            raise LeaseError(f"task {task_id} has no live lease to release")
        if reason == "merged":
            if task["state"] != "merged":
                raise LeaseError(f"task {task_id} is {task['state']}. Release as merged needs a merge record")
        elif not abandon:
            raise LeaseError(f"release of task {task_id} needs reason 'merged' or abandon")
        wt, branch, base = Path(task["worktree"]), task["branch"], task["base_commit"]
        if repository is None:
            raise LeaseError("release requires the repository path for trusted metadata")
        expected = task["head_commit"] or base
        try:
            git_dir, common_dir = pin_git_metadata(wt, repository)
        except GitError as exc:
            raise LeaseError(f"metadata refused release of task {task_id}: {exc}") from exc
        if not wt.is_dir():
            raise LeaseError(f"worktree {wt} is missing for task {task_id}")
        try:
            current = trusted_git(git_dir, common_dir, wt, "symbolic-ref", "-q", "--short", "HEAD", check=False)
            if current != branch:
                raise LeaseError(f"worktree {wt} HEAD is {current or 'detached'}, not branch {branch}")
            dirty = trusted_git(git_dir, common_dir, wt, "status", "--porcelain", "-uall")
            if dirty:
                raise LeaseError(f"worktree {wt} has {len(dirty.splitlines())} dirty or untracked path(s). "
                                 "The lease stays live")
            at = trusted_git(git_dir, common_dir, wt, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}", check=False)
            if at != expected:
                raise LeaseError(f"branch {branch} is at {at[:12] or 'nothing'}, expected {expected[:12]}")
            parked = f"jbi-parked/{task_id}-{uuid.uuid4().hex}"
            if not git(wt, "check-ref-format", "--branch", parked, check=False):
                raise LeaseError(f"parked ref name {parked!r} is not a valid git ref")
            if git(wt, "rev-parse", "--verify", "-q", f"refs/heads/{parked}", check=False):
                raise LeaseError(f"parked ref {parked} already exists")
            trusted_git(git_dir, common_dir, wt, "checkout", "--detach", base)
            try:
                trusted_git(git_dir, common_dir, wt, "branch", "-m", branch, parked)
            except GitError:
                trusted_git(git_dir, common_dir, wt, "checkout", branch, check=False)
                raise
        except GitError as exc:
            raise LeaseError(f"git refused release of task {task_id}: {exc}") from exc
        stamp = _now()
        final = "merged" if reason == "merged" else "abandoned"
        with self._tx() as db:
            lease = self._live_lease(task_id, db)
            if lease is None:
                raise LeaseError(f"task {task_id} lost its lease during release")
            db.execute("UPDATE leases SET released_at=?, release_reason=? WHERE id=?",
                       (stamp, reason, lease["id"]))
            db.execute("UPDATE tasks SET state=?, release_reason=?, updated_at=? WHERE id=?",
                       (final, reason, stamp, task_id))
            db.execute("UPDATE sessions SET status='idle', reset_at=?, updated_at=? WHERE id=?",
                       (stamp, stamp, lease["session_id"]))
            unpin_git_metadata(wt)
            return {"task": self._task(task_id, db), "parked_ref": parked,
                    "lease": dict(db.execute("SELECT * FROM leases WHERE id=?", (lease["id"],)).fetchone())}

    # ---- messages ----------------------------------------------------------------------

    def send(self, target: str, text: str, *, sender: str = "founder",
             recipients: Iterable[str] | None = None, all_sessions: bool = False) -> list[dict]:
        """Queue one message row for each known recipient. A queued row proves no remote action."""
        if not isinstance(text, str) or not text.strip():
            raise InvalidInput("message text is empty")
        if not isinstance(sender, str) or not sender.strip():
            raise InvalidInput("sender is empty")
        if all_sessions and target == "all":
            raise LeaseError("target 'all' with all_sessions is double intent. Use one of them")
        stamp = _now()
        with self._tx() as db:
            if all_sessions or target == "all":
                names = [r["id"] for r in db.execute("SELECT id FROM sessions ORDER BY id").fetchall()]
                if not names:
                    raise LeaseError("no registered session to receive the message")
            else:
                names = []
                for name in [target, *(recipients or [])]:
                    _check_id(name, "session")
                    if name not in names:
                        names.append(name)
                for name in names:
                    if db.execute("SELECT 1 FROM sessions WHERE id=?", (name,)).fetchone() is None:
                        raise LeaseError(f"session {name} is unknown")
            out = []
            for name in names:
                cur = db.execute("INSERT INTO messages (sender,target,text,created_at,delivery_state) "
                                 "VALUES (?,?,?,?,'queued')", (sender, name, text, stamp))
                out.append(dict(db.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()))
            return out

    # ---- cost --------------------------------------------------------------------------

    def record_cost(self, dispatch_id: int, *, input_tokens: int | None = None,
                    output_tokens: int | None = None, cache_read: int | None = None,
                    cache_write: int | None = None, cost_usd: float | None = None) -> None:
        """Upsert the usage of one dispatch. None means unknown and never erases a known value."""
        counts = [_count(v, n) for v, n in ((input_tokens, "input_tokens"), (output_tokens, "output_tokens"),
                                            (cache_read, "cache_read"), (cache_write, "cache_write"))]
        if cost_usd is not None and (isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float))
                                     or not math.isfinite(cost_usd) or cost_usd < 0):
            raise InvalidInput("cost_usd must be a nonnegative number or None")
        if isinstance(dispatch_id, bool) or not isinstance(dispatch_id, int):
            raise InvalidInput("dispatch_id must be an integer")
        with self._tx() as db:
            if db.execute("SELECT 1 FROM dispatches WHERE id=?", (dispatch_id,)).fetchone() is None:
                raise LeaseError(f"dispatch {dispatch_id} is unknown")
            db.execute(
                "INSERT INTO costs (dispatch_id,input_tokens,output_tokens,cache_read,cache_write,cost_usd,"
                "updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(dispatch_id) DO UPDATE SET "
                "input_tokens=COALESCE(excluded.input_tokens,input_tokens),"
                "output_tokens=COALESCE(excluded.output_tokens,output_tokens),"
                "cache_read=COALESCE(excluded.cache_read,cache_read),"
                "cache_write=COALESCE(excluded.cache_write,cache_write),"
                "cost_usd=COALESCE(excluded.cost_usd,cost_usd),updated_at=excluded.updated_at",
                (dispatch_id, *counts, cost_usd, _now()))

    # ---- CI and merge -------------------------------------------------------------------

    def record_ci(self, task_id: str, *, pr_number: int, head_sha: str, base_commit: str,
                  service_reply: dict, green_run: str | None = None,
                  squash_sha: str | None = None, state: str = "submitted") -> dict:
        """Insert one CI record. A green state needs a merge only through record_merge."""
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
            raise InvalidInput("pr_number must be a positive integer")
        _check_sha(head_sha, "head")
        _check_sha(base_commit, "base")
        if squash_sha is not None:
            _check_sha40(squash_sha, "squash")
        if not isinstance(service_reply, dict):
            raise InvalidInput("service_reply must be a dict")
        if state not in CI_STATES:
            raise InvalidInput(f"state must be one of {CI_STATES}")
        if green_run is not None and not isinstance(green_run, str):
            raise InvalidInput("green_run must be text or None")
        if state == "green" and (not green_run or not green_run.strip()
                                 or green_run.strip().lower() == "green"):
            raise InvalidInput("a green CI record needs run evidence text, not the word green")
        stamp = _now()
        with self._tx() as db:
            self._task(task_id, db)
            if state != "submitted":
                dup = None
                for old in db.execute("SELECT * FROM ci_submissions WHERE task_id=? AND pr_number=? "
                                      "AND head_sha=? AND state=? ORDER BY id", (task_id, pr_number, head_sha, state)):
                    if (old["green_run"] == green_run and old["service_reply"] == json.dumps(service_reply, sort_keys=True)
                            and squash_sha in (None, old["squash_sha"])):
                        dup = old["id"]
                        break
                if dup is not None:
                    self._settle(db, task_id, service_reply, state, stamp, keep=dup)
                    row = dict(db.execute("SELECT * FROM ci_submissions WHERE id=?", (dup,)).fetchone())
                    row["service_reply"] = json.loads(row["service_reply"])
                    return row
            cur = db.execute("INSERT INTO ci_submissions (task_id,pr_number,head_sha,base_commit,"
                             "service_reply,green_run,squash_sha,state,created_at,updated_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (task_id, pr_number, head_sha, base_commit,
                              json.dumps(service_reply, sort_keys=True), green_run, squash_sha, state,
                              stamp, stamp))
            if state != "submitted":
                self._settle(db, task_id, service_reply, state, stamp, keep=cur.lastrowid)
            row = dict(db.execute("SELECT * FROM ci_submissions WHERE id=?", (cur.lastrowid,)).fetchone())
            row["service_reply"] = json.loads(row["service_reply"])
            return row

    @staticmethod
    def _job_keys(task_id: str, reply: Any) -> set:
        """Return the job identities of one service reply: its job text and its bound PR and head."""
        keys: set = set()
        if not isinstance(reply, dict):
            return keys
        job = reply.get("job")
        if isinstance(job, str) and job.strip():
            keys.add(("job", job.strip()))
        bound = reply.get("bound")
        if isinstance(bound, dict) and bound.get("task") == task_id:
            head, pr = bound.get("head_sha"), bound.get("pr_number")
            if isinstance(head, str) and head.strip() and isinstance(pr, int) and not isinstance(pr, bool):
                keys.add(("bound", pr, head))
        return keys

    def _settle(self, db: sqlite3.Connection, task_id: str, reply: Any, state: str, stamp: str,
                keep: int) -> None:
        """Finish the earlier submitted rows of the same job. Other jobs and terminal rows stay."""
        wanted = self._job_keys(task_id, reply)
        if not wanted:
            return
        for row in db.execute("SELECT id, service_reply FROM ci_submissions WHERE task_id=? "
                              "AND state='submitted' AND id<>?", (task_id, keep)).fetchall():
            try:
                have = self._job_keys(task_id, json.loads(row["service_reply"]))
            except (TypeError, ValueError):
                continue
            if have & wanted:
                db.execute("UPDATE ci_submissions SET state=?, updated_at=? WHERE id=?",
                           (state, stamp, row["id"]))

    def pending_ci(self, task_id: str) -> list[dict]:
        """Return submitted CI records whose direct job evidence has not arrived."""
        _check_id(task_id, "task")
        rows = self._all("SELECT * FROM ci_submissions WHERE task_id=? AND state='submitted' "
                         "ORDER BY created_at", (task_id,))
        for row in rows:
            try:
                row["service_reply"] = json.loads(row["service_reply"])
            except (TypeError, ValueError):
                row["service_reply"] = {}
        return rows

    def record_merge(self, task_id: str, pr_number: int, head_sha: str, squash_sha: str,
                     green_run: str) -> dict:
        """Record one merge. It needs a green CI record with exact PR, head, and evidence."""
        _check_sha(head_sha, "head")
        if not isinstance(squash_sha, str) or not squash_sha.strip():
            raise LeaseError(f"merge of task {task_id} needs a squash sha")
        _check_sha40(squash_sha, "squash")
        if not isinstance(green_run, str) or not green_run.strip():
            raise LeaseError(f"merge of task {task_id} needs a green run")
        stamp = _now()
        with self._tx() as db:
            task = self._task(task_id, db)
            if task["state"] == "merged":
                prior = db.execute("SELECT * FROM merges WHERE task_id=?", (task_id,)).fetchone()
                if (prior is not None and prior["pr_number"] == pr_number and prior["head_sha"] == head_sha
                        and prior["squash_sha"] == squash_sha and prior["green_run"] == green_run):
                    return {"task": task, "merge": dict(prior)}
                raise LeaseError(f"task {task_id} is already merged with different evidence")
            rows = db.execute("SELECT * FROM ci_submissions WHERE task_id=? AND pr_number=? AND head_sha=? "
                              "AND state='green' ORDER BY id DESC", (task_id, pr_number, head_sha)).fetchall()
            good = [r for r in rows if (r["green_run"] or "").strip() == green_run
                    and json.loads(r["service_reply"]) and r["squash_sha"] in (None, squash_sha)]
            if not good:
                raise LeaseError(f"task {task_id} has no green CI record with evidence for PR {pr_number} "
                                 f"head {head_sha[:12]}")
            for row in good:
                self._settle(db, task_id, json.loads(row["service_reply"]), "green", stamp, keep=row["id"])
                if row["squash_sha"] is None:
                    db.execute("UPDATE ci_submissions SET squash_sha=?, updated_at=? WHERE id=?",
                               (squash_sha, stamp, row["id"]))
            db.execute("INSERT INTO merges (task_id,pr_number,head_sha,squash_sha,green_run,merged_at) "
                       "VALUES (?,?,?,?,?,?)", (task_id, pr_number, head_sha, squash_sha, green_run, stamp))
            db.execute("UPDATE tasks SET state='merged', updated_at=? WHERE id=?", (stamp, task_id))
            return {"task": self._task(task_id, db),
                    "merge": dict(db.execute("SELECT * FROM merges WHERE task_id=?", (task_id,)).fetchone())}

    # ---- stage persistence --------------------------------------------------------------

    def ensure_task(self, *, task_id: str, title: str, goal: str = "", owns: Iterable[str],
                    branch: str, worktree: Path, base_commit: str, node: str | None = None) -> dict:
        """Register one task before any agent runs. Repeat calls with equal fields return the record."""
        _check_id(task_id, "task")
        with self._lock:
            row = self._one("SELECT 1 FROM tasks WHERE id=?", (task_id,))
        if row is None:
            try:
                task = self.create_task(title=title, goal=goal, owns=owns, branch=branch,
                                        worktree=worktree, base_commit=base_commit,
                                        task_id=task_id, node=node)
            except LeaseError:
                if not self._one("SELECT 1 FROM tasks WHERE id=?", (task_id,)):
                    raise
            else:
                return task
        _check_sha(base_commit, "base")
        owned = normalize_owns(owns)
        task = self._task(task_id)
        for name, want, have in (("title", title, task["title"]), ("owns", owned, task["owns"]),
                                 ("branch", branch, task["branch"]),
                                 ("worktree", str(worktree), task["worktree"]),
                                 ("base_commit", base_commit, task["base_commit"])):
            if want != have:
                raise LeaseError(f"task {task_id} exists with a different {name}: "
                                 f"{have!r}, not {want!r}")
        return task

    def renew_dispatch(self, task_id: str, *, model: str | None = None,
                       brief_digest: str | None = None, verdict: str | None = None,
                       receipt: str | None = None, continuation: bool = True) -> dict:
        """Append one dispatch under the live lease. The lease, owns, and task state stay as they are."""
        _check_id(task_id, "task")
        for name, value in (("model", model), ("brief_digest", brief_digest),
                            ("verdict", verdict), ("receipt", receipt)):
            if value is not None and not isinstance(value, str):
                raise InvalidInput(f"{name} must be text or None")
        if not isinstance(continuation, bool):
            raise InvalidInput("continuation must be true or false")
        stamp = _now()
        with self._tx() as db:
            self._task(task_id, db)
            lease = self._live_lease(task_id, db)
            if lease is None:
                raise LeaseError(f"task {task_id} has no live lease to renew")
            db.execute("UPDATE dispatches SET finished_at=?, verdict='ended' "
                       "WHERE task_id=? AND finished_at IS NULL", (stamp, task_id))
            attempt = db.execute("SELECT COUNT(*) FROM dispatches WHERE task_id=?",
                                 (task_id,)).fetchone()[0] + 1
            cur = db.execute("INSERT INTO dispatches (task_id,session_id,attempt,model,continuation,brief_digest,"
                             "started_at,receipt,verdict) VALUES (?,?,?,?,?,?,?,?,?)",
                             (task_id, lease["session_id"], attempt, model, 1 if continuation else 0,
                              brief_digest, stamp, receipt, verdict))
            return {"dispatch_id": cur.lastrowid, "task": self._task(task_id, db),
                    "dispatch": dict(db.execute("SELECT * FROM dispatches WHERE id=?",
                                                (cur.lastrowid,)).fetchone()),
                    "lease": lease}

    def finish_dispatch(self, dispatch_id: int, *, verdict: str, receipt: str | None = None) -> None:
        """Close one dispatch with a verdict and an optional receipt."""
        if isinstance(dispatch_id, bool) or not isinstance(dispatch_id, int):
            raise InvalidInput("dispatch_id must be an integer")
        if not isinstance(verdict, str) or not verdict.strip():
            raise InvalidInput("verdict is empty")
        if receipt is not None and not isinstance(receipt, str):
            raise InvalidInput("receipt must be text or None")
        with self._tx() as db:
            if db.execute("SELECT 1 FROM dispatches WHERE id=?", (dispatch_id,)).fetchone() is None:
                raise LeaseError(f"dispatch {dispatch_id} is unknown")
            db.execute("UPDATE dispatches SET finished_at=?, verdict=?, receipt=COALESCE(?,receipt) "
                       "WHERE id=?", (_now(), verdict, receipt, dispatch_id))

    def record_session_history(self, session_id: str, history: list[dict], *,
                               status: str | None = None) -> None:
        """Store message history as compact sorted JSON. The caller keeps credentials out."""
        _check_id(session_id, "session")
        if not isinstance(history, list):
            raise InvalidInput("history must be a list")
        for i, entry in enumerate(history):
            if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                raise InvalidInput(f"history entry {i} needs role and content")
            if entry["role"] not in HISTORY_ROLES:
                raise InvalidInput(f"history entry {i} has bad role {entry['role']!r}")
            if not isinstance(entry["content"], (str, list, dict)):
                raise InvalidInput(f"history entry {i} content must be text, list, or dict")
        if status is not None and status not in SESSION_STATUSES:
            raise InvalidInput(f"status must be one of {SESSION_STATUSES}")
        try:
            blob = json.dumps(history, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise InvalidInput(f"history is not JSON: {exc}") from exc
        with self._tx() as db:
            if db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                raise LeaseError(f"session {session_id} is unknown")
            db.execute("UPDATE sessions SET history=?, status=COALESCE(?,status), updated_at=? WHERE id=?",
                       (blob, status, _now(), session_id))

    def session_history(self, session_id: str) -> list[dict]:
        """Return a fresh copy of the stored history. An unknown session gives an empty list."""
        _check_id(session_id, "session")
        row = self._one("SELECT history FROM sessions WHERE id=?", (session_id,))
        if row is None or not row["history"]:
            return []
        return json.loads(row["history"])

    def message_receipt(self, message_id: int, status: str, receipt: str = "") -> None:
        """Record a delivery state. A queued or failed row never moves to read directly."""
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise InvalidInput("message_id must be an integer")
        if status not in DELIVERY_STATES:
            raise InvalidInput(f"status must be one of {DELIVERY_STATES}")
        if not isinstance(receipt, str):
            raise InvalidInput("receipt must be text")
        with self._tx() as db:
            row = db.execute("SELECT delivery_state FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                raise LeaseError(f"message {message_id} is unknown")
            if status == "read" and row["delivery_state"] not in ("delivered", "read"):
                raise LeaseError(f"message {message_id} is {row['delivery_state']}. "
                                 "It must be delivered before read")
            db.execute("UPDATE messages SET delivery_state=?, receipt=? WHERE id=?",
                       (status, receipt, message_id))

    def pending_messages(self, session_id: str) -> list[dict]:
        """Return queued or sent rows for one session in id order. Read-only."""
        _check_id(session_id, "session")
        return self._all("SELECT * FROM messages WHERE target=? AND delivery_state IN ('queued','sent') ORDER BY id",
                         (session_id,))

    def mark_task_state(self, task_id: str, state: str) -> dict:
        """Set a task state. Only record_merge sets merged."""
        _check_id(task_id, "task")
        if state not in TASK_STATES:
            raise InvalidInput(f"state must be one of {TASK_STATES}")
        if state == "merged":
            raise LeaseError(f"task {task_id} cannot be set to merged here. record_merge is the only setter")
        with self._tx() as db:
            task = self._task(task_id, db)
            if task["state"] == "merged":
                raise LeaseError(f"task {task_id} is merged and keeps that state")
            if state == "dispatched" and self._live_lease(task_id, db) is None:
                raise LeaseError(f"task {task_id} has no live lease and cannot be dispatched")
            db.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?", (state, _now(), task_id))
            return self._task(task_id, db)

    def mark_session_status(self, session_id: str, status: str) -> None:
        """Set a session status. An unknown session raises."""
        _check_id(session_id, "session")
        if status not in SESSION_STATUSES:
            raise InvalidInput(f"status must be one of {SESSION_STATUSES}")
        with self._tx() as db:
            if db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                raise LeaseError(f"session {session_id} is unknown")
            db.execute("UPDATE sessions SET status=?, updated_at=? WHERE id=?",
                       (status, _now(), session_id))

    # ---- reads -------------------------------------------------------------------------

    def fleet(self) -> dict:
        """Return every record table and the cache totals as one JSON-compatible dict."""
        with self._lock:
            tasks = [self._task_dict(r) for r in self._all("SELECT * FROM tasks ORDER BY created_at, id")]
            leases = self._all("SELECT * FROM leases ORDER BY id")
            for lease in leases:
                lease["owns"] = json.loads(lease["owns"])
            ci = self._all("SELECT * FROM ci_submissions ORDER BY id")
            for row in ci:
                row["service_reply"] = json.loads(row["service_reply"])
            costs = self._all("SELECT * FROM costs ORDER BY dispatch_id")
            sessions = self._all("SELECT * FROM sessions ORDER BY id")
            dispatches = self._all("SELECT * FROM dispatches ORDER BY id")
            merges = self._all("SELECT * FROM merges ORDER BY id")
            messages = self._all("SELECT * FROM messages ORDER BY id")
        with self._lock:
            reads = [c["cache_read"] for c in costs if c["cache_read"] is not None]
            writes = [c["cache_write"] for c in costs if c["cache_write"] is not None]
        total_read = sum(reads) if reads else None
        total_write = sum(writes) if writes else None
        uncached = [c["input_tokens"] for c in costs if c["input_tokens"] is not None]
        uncached_total = sum(uncached) if uncached else None
        denom = ((total_read or 0) + (total_write or 0)
                 + (uncached_total or 0))
        rate = (total_read or 0) / denom if denom else None
        return {"sessions": sessions, "tasks": tasks, "dispatches": dispatches, "leases": leases,
                "ci_submissions": ci, "merges": merges, "messages": messages, "costs": costs,
                "cache_read": total_read, "cache_write": total_write,
                "uncached_input": uncached_total, "cache_hit_rate": rate}

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._db.close()
