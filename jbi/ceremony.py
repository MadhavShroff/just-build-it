"""The deterministic task ceremony and persistent window pool registry.

`run_ceremony` executes the fixed development pipeline for one task or a queue of tasks
without calling an LLM or waiting for human intervention:

1. Take a free tmux window from the persisted `WindowPool`.
2. Start a coding session in that window with `ticket start` semantics.
3. Hand the session the task brief.
4. Mark the window busy in the persisted registry.
5. Wait for the session's receipt on `sb`.
6. Push the branch and open (or reuse) a draft PR.
7. Call `illm ci submit` with the PR number and head SHA.
8. On a CI failure notice: re-brief the SAME window on `sb` with the structured failure
   record. The same model iterates with no model escalation and no decision card.
9. On green: mark the window free, record the task done in `TaskLedger`, and take the
   next task.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import briefs, ci_adapter as ci_mod, dag_loader, worktrees as wt
from .store import Changes, Store, now

TOKENS_RE = re.compile(r"\btokens\s*[:=]\s*(\d+)\b", re.IGNORECASE)
INPUT_TOKENS_RE = re.compile(r"\binput_tokens\s*[:=]\s*(\d+)\b", re.IGNORECASE)
OUTPUT_TOKENS_RE = re.compile(r"\boutput_tokens\s*[:=]\s*(\d+)\b", re.IGNORECASE)

MAX_ATTEMPTS = 8


class CeremonyError(RuntimeError):
    """A deterministic ceremony step failed or no free window was available."""


@dataclass(frozen=True)
class CeremonyReceipt:
    task: str
    sha: str
    passed: bool
    findings: list[str] = field(default_factory=list)
    tokens: int | None = None


@dataclass(frozen=True)
class TaskOutcome:
    task: str
    window: str
    pr_number: int
    head_sha: str
    merge_sha: str
    attempts: int
    tokens: int | None
    status: str = "done"


@dataclass(frozen=True)
class CeremonyOutcome:
    """Outcome of `run_ceremony`: exposes the first task's fields and iterates all tasks."""

    tasks: list[TaskOutcome]

    @property
    def task(self) -> str:
        return self.tasks[-1].task if self.tasks else ""

    @property
    def window(self) -> str:
        return self.tasks[-1].window if self.tasks else ""

    @property
    def pr_number(self) -> int:
        return self.tasks[-1].pr_number if self.tasks else 0

    @property
    def head_sha(self) -> str:
        return self.tasks[-1].head_sha if self.tasks else ""

    @property
    def merge_sha(self) -> str:
        return self.tasks[-1].merge_sha if self.tasks else ""

    @property
    def attempts(self) -> int:
        return self.tasks[-1].attempts if self.tasks else 0

    @property
    def tokens(self) -> int | None:
        return self.tasks[-1].tokens if self.tasks else None

    @property
    def status(self) -> str:
        return self.tasks[-1].status if self.tasks else "empty"

    def __len__(self) -> int:
        return len(self.tasks)

    def __iter__(self):
        return iter(self.tasks)

    def __getitem__(self, index: int) -> TaskOutcome:
        return self.tasks[index]


class WindowPool:
    """Persistent registry of tmux windows, their free/busy state, and held tasks."""

    def __init__(self, path: Path, windows: Iterable[str] = ()):
        self.path = Path(path)
        self._state: dict[str, Any] = {"updated": now(), "windows": {}}
        if self.path.exists():
            self._load()
        if windows:
            self.register(windows)
        elif not self.path.exists():
            self._save()

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("windows"), dict):
            raise ValueError(f"window registry {self.path} does not contain a valid 'windows' object")
        windows: dict[str, dict[str, Any]] = {}
        for name, entry in raw["windows"].items():
            if not isinstance(entry, dict):
                continue
            status = "busy" if entry.get("status") == "busy" else "free"
            windows[str(name)] = {
                "window": str(name),
                "status": status,
                "task": entry.get("task") if status == "busy" else None,
                "title": entry.get("title") if status == "busy" else None,
                "pr_number": entry.get("pr_number") if status == "busy" else None,
                "head_sha": entry.get("head_sha") if status == "busy" else None,
                "ci_status": entry.get("ci_status") if status == "busy" else None,
                "attempts": int(entry.get("attempts") or 0) if status == "busy" else 0,
                "brief": entry.get("brief") if status == "busy" else None,
                "started_at": entry.get("started_at") if status == "busy" else None,
                "updated_at": str(entry.get("updated_at") or raw.get("updated") or now()),
            }
        self._state = {"updated": str(raw.get("updated") or now()), "windows": windows}

    def _save(self) -> None:
        self._state["updated"] = now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def register(self, windows: Iterable[str]) -> None:
        """Ensure every window name exists in the registry without resetting busy windows."""
        changed = False
        stamp = now()
        for raw_name in windows:
            name = str(raw_name).strip()
            if not name or name in self._state["windows"]:
                continue
            self._state["windows"][name] = {
                "window": name,
                "status": "free",
                "task": None,
                "title": None,
                "pr_number": None,
                "head_sha": None,
                "ci_status": None,
                "attempts": 0,
                "brief": None,
                "started_at": None,
                "updated_at": stamp,
            }
            changed = True
        if changed or not self.path.exists():
            self._save()

    def take_free(self, task: str | None = None) -> str | None:
        """Return the window already holding `task`, or the first free window in the pool."""
        if task:
            for name, entry in self._state["windows"].items():
                if entry.get("status") == "busy" and entry.get("task") == task:
                    return name
        for name, entry in self._state["windows"].items():
            if entry.get("status") == "free":
                return name
        return None

    def mark_busy(self, window: str, task: str, *, title: str | None = None,
                  brief: str | None = None, attempt: int = 0,
                  pr_number: int | None = None, head_sha: str | None = None,
                  ci_status: str | None = None) -> dict[str, Any]:
        """Mark `window` busy with `task` and persist the registry."""
        existing = self._state["windows"].get(window) or {}
        if existing.get("status") == "busy" and existing.get("task") not in (None, task):
            raise CeremonyError(f"Window {window!r} is already busy with task {existing.get('task')!r}.")
        stamp = now()
        started_at = existing.get("started_at") if existing.get("task") == task and existing.get("started_at") else stamp
        record = {
            "window": window,
            "status": "busy",
            "task": task,
            "title": title if title is not None else existing.get("title"),
            "pr_number": pr_number if pr_number is not None else existing.get("pr_number"),
            "head_sha": head_sha if head_sha is not None else existing.get("head_sha"),
            "ci_status": ci_status if ci_status is not None else existing.get("ci_status"),
            "attempts": attempt,
            "brief": brief if brief is not None else existing.get("brief"),
            "started_at": started_at,
            "updated_at": stamp,
        }
        self._state["windows"][window] = record
        self._save()
        return dict(record)

    def update(self, window: str, **fields: Any) -> dict[str, Any]:
        """Update fields on an existing window entry and persist the registry."""
        if window not in self._state["windows"]:
            raise CeremonyError(f"Unknown window {window!r} in pool.")
        entry = self._state["windows"][window]
        for key, value in fields.items():
            entry[key] = value
        entry["updated_at"] = now()
        self._save()
        return dict(entry)

    def mark_free(self, window: str) -> dict[str, Any]:
        """Mark `window` free, clear its task and PR state, and persist the registry."""
        if window not in self._state["windows"]:
            raise CeremonyError(f"Unknown window {window!r} in pool.")
        stamp = now()
        record = {
            "window": window,
            "status": "free",
            "task": None,
            "title": None,
            "pr_number": None,
            "head_sha": None,
            "ci_status": None,
            "attempts": 0,
            "brief": None,
            "started_at": None,
            "updated_at": stamp,
        }
        self._state["windows"][window] = record
        self._save()
        return dict(record)

    def get(self, window: str) -> dict[str, Any] | None:
        entry = self._state["windows"].get(window)
        return dict(entry) if entry else None

    def snapshot(self) -> dict[str, Any]:
        windows = {k: dict(v) for k, v in self._state["windows"].items()}
        free = [v for v in windows.values() if v.get("status") == "free"]
        busy = [v for v in windows.values() if v.get("status") == "busy"]
        return {
            "path": str(self.path),
            "updated": self._state.get("updated"),
            "windows": windows,
            "free_count": len(free),
            "busy_count": len(busy),
        }


class TaskLedger:
    """Append-only JSONL task ledger recording completed tasks, tokens, and durations."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def record_done(self, *, task: str, window: str, pr_number: int | None = None,
                    head_sha: str = "", merge_sha: str | None = None,
                    title: str = "", attempts: int = 1, tokens: int | None = None,
                    started_at: str | None = None, completed_at: str | None = None,
                    duration_s: float | None = None) -> dict[str, Any]:
        done_ts = completed_at or now()
        if duration_s is None and started_at:
            duration_s = _elapsed_seconds(started_at, done_ts)
        entry = {
            "task": task,
            "title": title or task,
            "window": window,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "merge_sha": merge_sha or head_sha,
            "attempts": attempts,
            "tokens": tokens,
            "started_at": started_at,
            "completed_at": done_ts,
            "duration_s": duration_s,
            "status": "done",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for raw in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out


def _elapsed_seconds(start_iso: str, end_iso: str) -> float | None:
    try:
        start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return max(0.0, round((end_dt - start_dt).total_seconds(), 3))
    except ValueError:
        return None


def parse_sb_receipt(text: str, expected_task: str | None = None) -> CeremonyReceipt | None:
    """Parse a `BRIEF <task> | <sha> | PASS/FAIL` receipt from an `sb` message or receipt file."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        match = briefs.RECEIPT_HEAD.match(line)
        if not match:
            continue
        task_id, sha, verdict = match.group(1), match.group(2), match.group(3)
        if expected_task and task_id != expected_task:
            continue
        rest = lines[index + 1:]
        tokens = _extract_tokens(text)
        findings = [item for item in rest if not TOKENS_RE.fullmatch(item)]
        return CeremonyReceipt(task=task_id, sha=sha, passed=(verdict == "PASS"),
                               findings=findings, tokens=tokens)
    return None


def _extract_tokens(text: str) -> int | None:
    direct = TOKENS_RE.search(text)
    if direct:
        return int(direct.group(1))
    in_match = INPUT_TOKENS_RE.search(text)
    out_match = OUTPUT_TOKENS_RE.search(text)
    if in_match or out_match:
        return (int(in_match.group(1)) if in_match else 0) + (int(out_match.group(1)) if out_match else 0)
    return None


def poll_sb_receipt(window: str, task_id: str, attempt: int, receipt_file: Path, *,
                    board_db: Path | None = None, timeout: float = 3600.0,
                    poll_interval: float = 0.2, min_sb_id: int = 0) -> CeremonyReceipt:
    """Wait for a coding session receipt in `receipt_file` or in `board.db` `sb_messages`."""
    db_path = board_db or (Path.home() / ".tangram-dev" / "board.db")
    deadline = time.monotonic() + timeout
    while True:
        if receipt_file.is_file():
            parsed = parse_sb_receipt(receipt_file.read_text(encoding="utf-8", errors="replace"), task_id)
            if parsed is not None:
                return parsed
        if db_path.is_file():
            try:
                with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                    rows = conn.execute(
                        "SELECT id, text FROM sb_messages WHERE from_slot = ? AND id > ? ORDER BY id ASC",
                        (window, min_sb_id),
                    ).fetchall()
                for _, body in rows:
                    parsed = parse_sb_receipt(str(body or ""), task_id)
                    if parsed is not None:
                        return parsed
            except sqlite3.Error:
                pass
        if time.monotonic() >= deadline:
            raise CeremonyError(f"Timed out waiting for sb receipt from {window} for task {task_id} (attempt {attempt + 1}).")
        time.sleep(poll_interval)


def poll_ci_notice(tid: str, pr_number: int, head_sha: str, attempt: int, *,
                   run_dir: Path, repo: Path, runner: ci_mod.Runner = ci_mod.default_runner,
                   illm_state_dir: Path | None = None, timeout: float = 3600.0,
                   poll_interval: float = 1.0, status_interval: float = 15.0,
                   seen_notices: set[str] | None = None) -> dict[str, Any]:
    """Wait for a CI verdict or structured failure notice for `(pr_number, head_sha)`."""
    state_root = illm_state_dir or Path(
        os.environ.get("ILLM_STATE_DIR", str(Path.home() / ".local" / "state" / "interface-llm"))
    )
    seen = seen_notices if seen_notices is not None else set()
    deadline = time.monotonic() + timeout
    last_status_at: float | None = None
    while True:
        # 1. Check run_dir notice file or JSONL queue if present.
        attempt_file = run_dir / "notices" / f"{tid}-{attempt}.json"
        if attempt_file.is_file():
            try:
                rec = json.loads(attempt_file.read_text(encoding="utf-8"))
                if isinstance(rec, dict):
                    return _normalize_ci_notice(rec, pr_number, head_sha)
            except (OSError, ValueError):
                pass

        # 2. Check interface-llm mailbox queues on disk.
        mailboxes = state_root / "mailboxes"
        if mailboxes.is_dir():
            for qfile in sorted(mailboxes.glob("*/queue/*.json")):
                key = str(qfile)
                if key in seen:
                    continue
                try:
                    rec = json.loads(qfile.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(rec, dict) or rec.get("from") not in (None, "ci"):
                    continue
                matched = _match_mailbox_notice(rec, tid, pr_number, head_sha)
                if matched is not None:
                    seen.add(key)
                    return matched

        # 3. Query `illm --json ci status <pr_number>` at most once per status_interval.
        now_mono = time.monotonic()
        if last_status_at is None or (now_mono - last_status_at) >= status_interval:
            last_status_at = now_mono
            res = runner(["illm", "--json", "ci", "status", str(pr_number)], repo)
            if res.returncode == 0 and (res.stdout or "").strip():
                try:
                    payload = json.loads(res.stdout.strip())
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    matched = _match_status_payload(payload, pr_number, head_sha)
                    if matched is not None:
                        return matched

        if time.monotonic() >= deadline:
            raise CeremonyError(
                f"Timed out waiting for CI notice for task {tid} PR #{pr_number} @ {head_sha[:10]}."
            )
        time.sleep(poll_interval)


def _normalize_ci_notice(raw: dict[str, Any], pr_number: int, head_sha: str) -> dict[str, Any]:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    merged = {**data, **{k: v for k, v in raw.items() if k != "data"}}
    ntype = str(merged.get("type") or "")
    verdict = merged.get("verdict")
    if not verdict:
        if ntype == "done" or merged.get("status") in ("merged", "green"):
            verdict = "merged"
        elif ntype in ("failed", "needs-input") or merged.get("status") in ("red", "failed"):
            verdict = "red"
    merged.setdefault("pr_number", pr_number)
    merged.setdefault("head_sha", head_sha)
    if verdict:
        merged["verdict"] = verdict
    if "first_failure_excerpt" not in merged and isinstance(merged.get("text"), str) and verdict not in ("green", "merged"):
        merged["first_failure_excerpt"] = merged["text"]
    return merged


def _match_mailbox_notice(rec: dict[str, Any], tid: str, pr_number: int, head_sha: str) -> dict[str, Any] | None:
    norm = _normalize_ci_notice(rec, pr_number, head_sha)
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    rec_pr = data.get("pr_number") if "pr_number" in data else rec.get("pr_number")
    rec_sha = data.get("head_sha") if "head_sha" in data else rec.get("head_sha")
    rec_task = data.get("task") if "task" in data else rec.get("task")
    text = str(rec.get("text") or "")
    if rec_pr is not None and int(rec_pr) != int(pr_number):
        return None
    if rec_sha and head_sha and str(rec_sha) != str(head_sha):
        return None
    if rec_pr is None and rec_task != tid and f"#{pr_number}" not in text and str(pr_number) not in text:
        return None
    if norm.get("verdict") in ("green", "merged", "red", "void", "merge-blocked"):
        return norm
    return None


def _match_status_payload(payload: dict[str, Any], pr_number: int, head_sha: str) -> dict[str, Any] | None:
    norm = _normalize_ci_notice(payload, pr_number, head_sha)
    rec_sha = norm.get("head_sha")
    if rec_sha and head_sha and str(rec_sha) != str(head_sha):
        return None
    if norm.get("verdict") in ("green", "merged", "red", "void", "merge-blocked"):
        return norm
    return None


def _exec_cmd(runner: ci_mod.Runner, argv: list[str], cwd: Path) -> str:
    res = runner(argv, cwd)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise CeremonyError(f"{' '.join(argv[:3])} failed (exit {res.returncode}): {detail}")
    return (res.stdout or "").strip()


def _normalize_tasks(task_or_tasks: dict[str, Any] | Iterable[dict[str, Any]],
                     next_tasks: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(task_or_tasks, dict):
        if "tasks" in task_or_tasks and isinstance(task_or_tasks["tasks"], dict):
            order = task_or_tasks.get("order") or list(task_or_tasks["tasks"])
            items.extend(task_or_tasks["tasks"][tid] for tid in order if tid in task_or_tasks["tasks"])
        else:
            items.append(task_or_tasks)
    else:
        items.extend(task_or_tasks)
    items.extend(next_tasks)
    return items


def run_ceremony(
    task_or_tasks: dict[str, Any] | Iterable[dict[str, Any]],
    *,
    pool: WindowPool,
    repo: Path,
    run_dir: Path,
    ledger: TaskLedger | None = None,
    store: Store | None = None,
    runner: ci_mod.Runner = ci_mod.default_runner,
    ci_adapter: ci_mod.CIAdapter | None = None,
    wait_receipt: Callable[[str, str, int, Path], CeremonyReceipt] | None = None,
    wait_ci_notice: Callable[[str, int, str, int], dict[str, Any]] | None = None,
    base: str = wt.MAIN,
    next_tasks: Iterable[dict[str, Any]] = (),
    max_attempts: int = MAX_ATTEMPTS,
    reset_window_every: int | None = None,
) -> CeremonyOutcome:
    """Run the fixed 9-step ceremony deterministically for one task or a sequence of tasks."""
    if max_attempts < 1:
        raise CeremonyError(f"max_attempts must be at least 1, got {max_attempts}.")
    if reset_window_every is not None and reset_window_every < 1:
        raise CeremonyError(f"reset_window_every must be at least 1, got {reset_window_every}.")
    tasks = _normalize_tasks(task_or_tasks, next_tasks)
    run_dir = Path(run_dir)
    repo = Path(repo)
    ledger = ledger or TaskLedger(run_dir / "task_ledger.jsonl")
    ci = ci_adapter or ci_mod.CIAdapter(runner=runner)
    receipt_waiter = wait_receipt or (lambda w, t, a, p: poll_sb_receipt(w, t, a, p))
    seen_notices: set[str] = set()
    ci_waiter = wait_ci_notice or (
        lambda t, pr, sha, a: poll_ci_notice(
            t, pr, sha, a, run_dir=run_dir, repo=repo, runner=runner, seen_notices=seen_notices
        )
    )

    outcomes: list[TaskOutcome] = []
    has_git = (repo / ".git").exists()

    for raw_task in tasks:
        task = dict(raw_task)
        tid = str(task["id"])
        title = str(task.get("title") or tid)
        goal = str(task.get("goal") or title)
        branch = str(task.get("branch") or f"jbi/{tid}")
        started_at = now()

        # 1. Take a free tmux window from the pool.
        window = pool.take_free(tid)
        if window is None:
            raise CeremonyError(f"No free tmux window in pool for task {tid!r}.")

        worktree = repo
        if has_git:
            base_sha = wt.head(repo, base)
            if base_sha is not None:
                worktree = wt.create_worktree(repo, tid, base_sha, run_dir / "worktrees")

        attempt = 0
        total_tokens = 0
        saw_tokens = False

        def _abort(failed_attempts: int, findings: list[str], *, reason: str = "") -> None:
            pool.mark_free(window)
            if store is not None and store.read(f"/plan/tasks/{tid}") is not None:
                store.commit("task.status", Changes()
                             .set("plan", "tasks", tid, "attempts", failed_attempts)
                             .set("plan", "tasks", tid, "findings", findings)
                             .set("plan", "tasks", tid, "status", "failed")
                             .set("plan", "tasks", tid, "window", None), {"task": tid})
            noun = "attempt" if failed_attempts == 1 else "attempts"
            parts = [p for p in (reason, "; ".join(findings)) if p]
            suffix = f": {'; '.join(parts)}" if parts else "."
            raise CeremonyError(f"Task {tid} failed after {failed_attempts} {noun}{suffix}")

        def _rebrief_or_abort(
            next_attempt: int, findings: list[str], *, ci_failed: bool = False
        ) -> tuple[Path, Path]:
            if next_attempt >= max_attempts:
                _abort(next_attempt, findings)
            next_brief, next_receipt = briefs.write(
                run_dir, task=task, role="implement", stage="implement",
                attempt=next_attempt, findings=findings,
            )
            pool_fields: dict[str, Any] = {"attempts": next_attempt, "brief": str(next_brief)}
            if ci_failed:
                pool_fields["ci_status"] = "failed"
            pool.update(window, **pool_fields)
            if ci_failed and store is not None and store.read(f"/plan/tasks/{tid}") is not None:
                store.commit("task.status", Changes()
                             .set("plan", "tasks", tid, "attempts", next_attempt)
                             .set("plan", "tasks", tid, "findings", findings)
                             .set("plan", "tasks", tid, "status", "running"), {"task": tid})
            if reset_window_every is not None and next_attempt > 0 and next_attempt % reset_window_every == 0:
                cmd = ["ticket", "start", window, "--file", str(next_brief)]
            else:
                cmd = ["sb", "send", window, "--file", str(next_brief)]
            _exec_cmd(runner, cmd, repo)
            return next_brief, next_receipt

        # 2. Start a coding session there with `ticket start` semantics.
        # 3. Hand the session the task brief.
        brief_file, receipt_file = briefs.write(
            run_dir, task=task, role="implement", stage="implement", attempt=attempt
        )
        _exec_cmd(runner, ["ticket", "start", window, "--file", str(brief_file)], repo)

        # 4. Mark the window busy.
        pool.mark_busy(window, tid, title=title, brief=str(brief_file), attempt=attempt)
        if store is not None and store.read(f"/plan/tasks/{tid}") is not None:
            store.commit("task.status", Changes()
                         .set("plan", "tasks", tid, "status", "running")
                         .set("plan", "tasks", tid, "window", window), {"task": tid})

        while True:
            # 5. Wait for the session's receipt on `sb`.
            receipt = receipt_waiter(window, tid, attempt, receipt_file)
            if isinstance(receipt, briefs.Receipt):
                receipt = CeremonyReceipt(task=receipt.task, sha=receipt.sha,
                                          passed=receipt.passed, findings=list(receipt.findings))
            if receipt.tokens is not None:
                total_tokens += receipt.tokens
                saw_tokens = True

            owns = task.get("owns") or []
            if has_git and owns and (worktree / ".git").exists():
                unowned = [p for p in wt.dirty_paths(worktree) if not wt.path_owned(p, owns)]
                if unowned:
                    wt.discard_changes(worktree)
                    attempt += 1
                    findings = [f"modified unowned path {', '.join(unowned)}"]
                    brief_file, receipt_file = _rebrief_or_abort(attempt, findings)
                    continue

            if not receipt.passed:
                attempt += 1
                findings = list(receipt.findings) or [f"{tid} reported FAIL on attempt {attempt}"]
                brief_file, receipt_file = _rebrief_or_abort(attempt, findings)
                continue

            head_sha = ""
            if has_git and (worktree / ".git").exists():
                committed = wt.commit_all(worktree, f"{tid}: implement attempt {attempt + 1}")
                head_sha = committed or wt.head(worktree, "HEAD") or ""
            if not head_sha and receipt.sha and receipt.sha != "uncommitted":
                head_sha = receipt.sha
            if not head_sha and has_git:
                head_sha = wt.head(repo, branch) or wt.head(repo, base) or ""
            if not head_sha:
                raise CeremonyError(f"Task {tid} produced no commit SHA for branch {branch}.")

            # 6. Push the branch and open a draft PR.
            ci.push_branch(repo, branch)
            pr_number = ci.find_or_create_draft_pr(repo, branch=branch, base=base, title=title, body=goal)

            # 7. Call `illm ci submit` with the PR number and head sha.
            ci.submit(repo, pr_number, head_sha)
            pool.update(window, pr_number=pr_number, head_sha=head_sha,
                        ci_status="submitted", attempts=attempt)
            if store is not None and store.read(f"/plan/tasks/{tid}") is not None:
                store.commit("task.status", Changes()
                             .set("plan", "tasks", tid, "pr_number", pr_number)
                             .set("plan", "tasks", tid, "commit", head_sha)
                             .set("plan", "tasks", tid, "status", "merging"), {"task": tid})

            # 8. Wait for the CI verdict; on failure re-brief, reset, or stop.
            raw_notice = ci_waiter(tid, pr_number, head_sha, attempt)
            raw_data = raw_notice.get("data") if isinstance(raw_notice.get("data"), dict) else {}
            notice = {**raw_data, **{k: v for k, v in raw_notice.items() if k != "data" and v is not None}}
            verdict = str(notice.get("verdict") or "red")
            if verdict not in ("green", "merged"):
                findings = ci_mod.format_failure_findings(notice)
                attempt += 1
                if notice.get("retry_allowed") is False:
                    _abort(attempt, findings, reason="CI marked notice non-retryable")
                brief_file, receipt_file = _rebrief_or_abort(attempt, findings, ci_failed=True)
                continue

            # 9. On green: mark the window free, record the task done, take the next task.
            merge_sha = str(notice.get("merge_sha") or notice.get("head_sha") or head_sha)
            pool.mark_free(window)
            tokens_val = total_tokens if saw_tokens else (
                int(notice["tokens"]) if isinstance(notice.get("tokens"), int) else None
            )
            ledger.record_done(
                task=tid, title=title, window=window, pr_number=pr_number,
                head_sha=head_sha, merge_sha=merge_sha, attempts=attempt + 1,
                tokens=tokens_val, started_at=started_at, completed_at=now(),
            )
            if store is not None and store.read(f"/plan/tasks/{tid}") is not None:
                stages = dict(store.read(f"/plan/tasks/{tid}/stages") or {})
                stages["merge"] = {"status": "passed", "detail": "merged", "model": None}
                store.commit("task.status", Changes()
                             .set("plan", "tasks", tid, "status", "merged")
                             .set("plan", "tasks", tid, "commit", merge_sha)
                             .set("plan", "tasks", tid, "stages", stages), {"task": tid})
            outcomes.append(TaskOutcome(
                task=tid, window=window, pr_number=pr_number, head_sha=head_sha,
                merge_sha=merge_sha, attempts=attempt + 1, tokens=tokens_val, status="done",
            ))
            break

    return CeremonyOutcome(tasks=outcomes)


def main(argv: list[str] | None = None, *, runner: ci_mod.Runner = ci_mod.default_runner,
         board_db: Path | None = None, illm_state_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jbi-ceremony", description="Run the deterministic task ceremony.")
    parser.add_argument("--dag", type=Path, required=True, help="path to task DAG (.json or restricted YAML)")
    parser.add_argument("--repo", type=Path, required=True, help="target git repository")
    parser.add_argument("--run-dir", type=Path, required=True, help="directory for briefs, receipts, pool and ledger")
    parser.add_argument("--windows", required=True, help="comma-separated tmux window names for the pool")
    parser.add_argument("--base", default=wt.MAIN, help="base branch for draft PRs")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS,
                        help="maximum attempts per task before aborting")
    parser.add_argument("--reset-window-every", type=int, default=None,
                        help="reset window via ticket start every N failed attempts")
    args = parser.parse_args(argv)

    run_dir = args.run_dir.expanduser().resolve()
    repo = args.repo.expanduser().resolve()
    plan = dag_loader.load(args.dag.expanduser().resolve())
    windows = [w.strip() for w in args.windows.split(",") if w.strip()]
    pool = WindowPool(run_dir / "window_pool.json", windows=windows)
    ledger = TaskLedger(run_dir / "task_ledger.jsonl")
    seen_notices: set[str] = set()
    run_ceremony(
        plan,
        pool=pool,
        ledger=ledger,
        repo=repo,
        run_dir=run_dir,
        base=args.base,
        runner=runner,
        max_attempts=args.max_attempts,
        reset_window_every=args.reset_window_every,
        wait_receipt=lambda w, t, a, p: poll_sb_receipt(w, t, a, p, board_db=board_db),
        wait_ci_notice=lambda t, pr, sha, a: poll_ci_notice(
            t, pr, sha, a, run_dir=run_dir, repo=repo, runner=runner,
            illm_state_dir=illm_state_dir, seen_notices=seen_notices,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
