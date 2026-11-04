"""The run store: one SQLite event log and the state document that it produces.

Every change goes through `Store.commit`. A commit applies JSON Patch operations to
a copy of the state, appends one event with the next sequence number, and wakes every
waiting reader. A failed patch changes nothing. On restart the store loads the latest
snapshot and replays the events after it, so the state survives a crash.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import jsonpatch as jp

SNAPSHOT_EVERY = 200
TIMELINE_LIMIT = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, type TEXT NOT NULL,
  ops TEXT NOT NULL, meta TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (seq INTEGER PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, payload TEXT NOT NULL,
  result TEXT NOT NULL, ts TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Changes:
    """Collects patch operations for one event. Paths are built from raw tokens."""

    def __init__(self) -> None:
        self.ops: list[dict] = []

    def set(self, *path_and_value: Any) -> "Changes":
        *tokens, value = path_and_value
        self.ops.append(jp.add(jp.pointer(*tokens), value))
        return self

    def replace(self, *path_and_value: Any) -> "Changes":
        *tokens, value = path_and_value
        self.ops.append(jp.replace(jp.pointer(*tokens), value))
        return self

    def remove(self, *tokens: Any) -> "Changes":
        self.ops.append(jp.remove(jp.pointer(*tokens)))
        return self

    def append(self, *path_and_value: Any) -> "Changes":
        *tokens, value = path_and_value
        self.ops.append(jp.add(jp.pointer(*tokens) + "/-", value))
        return self

    def __bool__(self) -> bool:
        return bool(self.ops)


class Store:
    def __init__(self, path: Path, initial: dict | None = None):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._closed = False
        self.state, self.seq = self._load()
        if self.seq == 0:
            if initial is None:
                raise ValueError(f"{self.path} holds no run and no initial state was given")
            self.commit("run.created", [jp.replace("", initial)])

    # ---- reading -------------------------------------------------------------------

    def _load(self) -> tuple[dict, int]:
        row = self._db.execute("SELECT seq, state FROM snapshots ORDER BY seq DESC LIMIT 1").fetchone()
        state, seq = (json.loads(row[1]), row[0]) if row else ({}, 0)
        for s, ops in self._db.execute("SELECT seq, ops FROM events WHERE seq > ? ORDER BY seq", (seq,)):
            state, seq = jp.apply(state, json.loads(ops)), s
        return state, seq

    def snapshot(self) -> tuple[int, dict]:
        with self._lock:
            return self.seq, copy.deepcopy(self.state)

    def read(self, path: str, default: Any = None) -> Any:
        """Read a value by JSON pointer. Returns a deep copy, so callers cannot mutate state."""
        with self._lock:
            return copy.deepcopy(jp.get(self.state, path, default))

    def events_after(self, seq: int, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, ts, type, ops, meta FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                (seq, limit)).fetchall()
        return [{"seq": s, "ts": ts, "type": t, "ops": json.loads(o), "meta": json.loads(m)}
                for s, ts, t, o, m in rows]

    def wait(self, seq: int, timeout: float) -> int:
        """Block until the store passes `seq`, the timeout ends, or the store closes."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while self.seq <= seq and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            return self.seq

    # ---- writing -------------------------------------------------------------------

    def commit(self, etype: str, ops: list[dict] | Changes, meta: dict | None = None) -> dict:
        ops = ops.ops if isinstance(ops, Changes) else list(ops)
        meta = meta or {}
        with self._lock:
            candidate = jp.apply(copy.deepcopy(self.state), ops)
            event = {"seq": self.seq + 1, "ts": now(), "type": etype, "ops": ops, "meta": meta}
            self._db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                             (event["seq"], event["ts"], etype, json.dumps(ops), json.dumps(meta)))
            self.state, self.seq = candidate, event["seq"]
            if self.seq % SNAPSHOT_EVERY == 0:
                self._db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?)",
                                 (self.seq, json.dumps(self.state)))
            self._changed.notify_all()
            return event

    def log(self, kind: str, text: str, *, extra: Changes | None = None,
            etype: str = "log.line", **meta: Any) -> dict:
        """Commit an event that also appends one line to the timeline."""
        changes = extra or Changes()
        with self._lock:
            entry = {"seq": self.seq + 1, "ts": now(), "kind": kind, "text": text}
            entry.update({k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))})
            changes.append("timeline", entry)
            overflow = len(self.state.get("timeline", [])) + 1 - TIMELINE_LIMIT
            for _ in range(max(0, overflow)):
                changes.remove("timeline", 0)
            return self.commit(etype, changes, {"kind": kind, **meta})

    # ---- command de-duplication -----------------------------------------------------

    def command_result(self, cid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT result FROM commands WHERE id = ?", (cid,)).fetchone()
        return json.loads(row[0]) if row else None

    def record_command(self, cid: str, ctype: str, payload: dict, result: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO commands VALUES (?, ?, ?, ?, ?)",
                             (cid, ctype, json.dumps(payload), json.dumps(result), now()))

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        with self._changed:
            self._closed = True
            self._changed.notify_all()
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?)", (self.seq, json.dumps(self.state)))
            self._db.close()


def iter_sse(store: Store, after: int, *, heartbeat: float = 15.0,
             stop: threading.Event | None = None) -> Iterator[bytes]:
    """Yield Server-Sent Events frames for every event after `after`."""
    cursor = after
    first = store.events_after(0, 1)
    if after > 0 and first and first[0]["seq"] > after + 1:
        yield b"event: reset\ndata: {}\n\n"
        return
    while not (stop and stop.is_set()):
        batch = store.events_after(cursor)
        if batch:
            for event in batch:
                data = json.dumps(event, separators=(",", ":"))
                yield f"id: {event['seq']}\nevent: patch\ndata: {data}\n\n".encode()
                cursor = event["seq"]
            continue
        if store.wait(cursor, heartbeat) <= cursor:
            if store._closed:
                return
            yield b": ping\n\n"
