"""The internal task dispatch lifecycle and the persistent window pool registry.

`run_ceremony` executes the fixed development pipeline for one task or a queue of tasks
without an external messaging service and without human intervention:

1. Take a free window from the persisted `WindowPool`.
2. Create the task worktree and register the task in `Tracking`.
3. Dispatch a lease, then run the leased claude-mux-lite harness with the task brief.
4. Read the agent receipt. A FAIL or an unowned change starts a same-task correction.
5. Commit the work, then hand over to CI with `CIAdapter.handover`.
6. Call `CIAdapter.confirm_merge`. Only a verified squash commit marks the task merged.
7. A CI failure starts a fresh correction dispatch under the same lease, up to `max_attempts`.
8. On a verified merge: release the lease, free the window, record the task in `TaskLedger`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import briefs, ci_adapter as ci_mod, dag_loader, worktrees as wt
from .store import Changes, Store, now
from .tracking import LeaseError, Tracking

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_MODEL = "claude-sonnet-5-5"
TOKENS_RE = re.compile(r"\btokens\s*[:=]\s*(\d+)\b", re.IGNORECASE)
INPUT_TOKENS_RE = re.compile(r"\binput_tokens\s*[:=]\s*(\d+)\b", re.IGNORECASE)
OUTPUT_TOKENS_RE = re.compile(r"\boutput_tokens\s*[:=]\s*(\d+)\b", re.IGNORECASE)

MAX_ATTEMPTS = 8


class CeremonyError(RuntimeError):
    """A lifecycle step failed or no free window was available."""


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


def parse_agent_receipt(text: str, expected_task: str | None = None) -> CeremonyReceipt | None:
    """Parse a `BRIEF <task> | <sha> | PASS/FAIL` receipt from a receipt file or final agent text."""
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


def default_harness_factory(**kwargs: Any) -> Any:
    """Build the local claude-mux-lite harness. Tests inject a replacement."""
    from claude_mux_lite.harness import LiteHarness
    return LiteHarness(**kwargs)


def _full_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA40_RE.match(value))


def run_ceremony(
    task_or_tasks: dict[str, Any] | Iterable[dict[str, Any]],
    *,
    pool: WindowPool,
    repo: Path,
    run_dir: Path,
    ledger: TaskLedger | None = None,
    store: Store | None = None,
    tracking: Tracking | None = None,
    runner: ci_mod.Runner = ci_mod.default_runner,
    ci: ci_mod.CIAdapter | None = None,
    harness_factory: Callable[..., Any] | None = None,
    model: str = DEFAULT_MODEL,
    base: str = wt.MAIN,
    next_tasks: Iterable[dict[str, Any]] = (),
    max_attempts: int = MAX_ATTEMPTS,
    reset_window_every: int | None = None,
) -> CeremonyOutcome:
    """Run the internal dispatch lifecycle for one task or a sequence of tasks.

    For each task: take a free window, create the worktree, register the task, dispatch a
    lease, run the leased harness, commit, hand over to CI through `CIAdapter`, confirm a
    verified squash merge, and release. A failed receipt or failed CI starts a fresh
    same-task correction dispatch until `max_attempts`."""
    if max_attempts < 1:
        raise CeremonyError(f"max_attempts must be at least 1, got {max_attempts}.")
    if reset_window_every is not None and reset_window_every < 1:
        raise CeremonyError(f"reset_window_every must be at least 1, got {reset_window_every}.")
    tasks = _normalize_tasks(task_or_tasks, next_tasks)
    run_dir = Path(run_dir).resolve()
    repo = Path(repo)
    ledger = ledger or TaskLedger(run_dir / "task_ledger.jsonl")
    ci = ci if ci is not None else ci_mod.CIAdapter(runner=runner)
    factory = harness_factory if harness_factory is not None else default_harness_factory
    own_tracking = tracking is None
    tracking = tracking if tracking is not None else Tracking(run_dir / "fleet.db")
    outcomes: list[TaskOutcome] = []
    try:
        for raw_task in tasks:
            outcomes.append(_run_task(
                dict(raw_task), pool=pool, repo=repo, run_dir=run_dir, ledger=ledger, store=store,
                tracking=tracking, ci=ci, factory=factory, model=model, base=base,
                max_attempts=max_attempts, reset_window_every=reset_window_every))
    finally:
        if own_tracking:
            tracking.close()
    return CeremonyOutcome(tasks=outcomes)


def _run_task(task: dict[str, Any], *, pool: WindowPool, repo: Path, run_dir: Path, ledger: TaskLedger,
              store: Store | None, tracking: Tracking, ci: ci_mod.CIAdapter, factory: Callable[..., Any],
              model: str, base: str, max_attempts: int, reset_window_every: int | None) -> TaskOutcome:
    tid = str(task["id"])
    title = str(task.get("title") or tid)
    goal = str(task.get("goal") or title)
    owns = list(task.get("owns") or [])
    if not owns:
        raise CeremonyError(f"Task {tid} declares no owns paths. A lease needs at least one.")
    branch = f"jbi/{tid}"
    task["branch"] = branch
    started_at = now()

    window = pool.take_free(tid)
    if window is None:
        raise CeremonyError(f"No free window in pool for task {tid!r}.")
    if not (repo / ".git").exists():
        raise CeremonyError(f"Task {tid} needs a git repository at {repo}.")
    base_sha = wt.head(repo, base)
    if not _full_sha(base_sha):
        raise CeremonyError(f"Base {base!r} in {repo} has no full commit for task {tid}.")
    worktree = wt.create_worktree(repo, tid, base_sha, run_dir / "worktrees")

    def store_set(**fields: Any) -> None:
        if store is None or store.read(f"/plan/tasks/{tid}") is None:
            return
        changes = Changes()
        for key, value in fields.items():
            changes = changes.set("plan", "tasks", tid, key, value)
        store.commit("task.status", changes, {"task": tid})

    session_id = f"{window}-{tid}"[:64]
    tracking.ensure_task(task_id=tid, title=title, goal=goal, owns=owns, branch=branch,
                         worktree=worktree, base_commit=base_sha)

    attempt = 0
    total_tokens = 0
    saw_tokens = False

    def abort(failed_attempts: int, findings: list[str], *, reason: str = "") -> None:
        note = ""
        try:
            tracking.release(tid, reason="abandon", abandon=True)
            pool.mark_free(window)
        except LeaseError as error:
            note = f" Release refused, the lease stays live: {error}"
        store_set(attempts=failed_attempts, findings=findings, status="failed",
                  window=None if not note else window)
        noun = "attempt" if failed_attempts == 1 else "attempts"
        parts = [p for p in (reason, "; ".join(findings)) if p]
        suffix = f": {'; '.join(parts)}" if parts else "."
        raise CeremonyError(f"Task {tid} failed after {failed_attempts} {noun}{suffix}{note}")

    def write_brief(findings: list[str] | None = None) -> tuple[Path, Path]:
        brief_file, receipt_file = briefs.write(
            run_dir, task=task, role="implement", stage="implement", attempt=attempt, findings=findings)
        return brief_file, receipt_file.resolve()

    def run_dispatch(dispatch_id: int, continuation: bool, brief_file: Path,
                     receipt_file: Path) -> CeremonyReceipt:
        history = tracking.session_history(session_id) if continuation else []
        checkpoint = run_dir / "checkpoints" / f"{tid}-{attempt}.json"
        harness = factory(workdir=worktree, model=model, task_id=tid, tracking=tracking,
                          owns=tuple(owns), session=history, receipt=receipt_file, checkpoint=checkpoint)
        result = harness.run(brief_file.read_text(encoding="utf-8"))
        usage = getattr(result, "usage", None)
        counts = {name: getattr(usage, name, None)
                  for name in ("input_tokens", "output_tokens", "cache_read", "cache_write")}
        tracking.record_cost(dispatch_id, **counts)
        take = getattr(harness, "take_messages", None)
        if callable(take):
            tracking.record_session_history(session_id, take())
        text = str(getattr(result, "final_text", "") or "")
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        known = [counts[k] for k in ("input_tokens", "output_tokens") if counts[k] is not None]
        tokens = sum(known) if known else None
        if exit_code != 0:
            tracking.finish_dispatch(dispatch_id, verdict="error", receipt=text[:2000] or None)
            finding = text.strip().splitlines()[0] if text.strip() else f"agent exited {exit_code}"
            return CeremonyReceipt(task=tid, sha="uncommitted", passed=False,
                                   findings=[f"agent exit {exit_code}: {finding}"], tokens=tokens)
        parsed = None
        if receipt_file.is_file():
            parsed = parse_agent_receipt(receipt_file.read_text(encoding="utf-8", errors="replace"), tid)
        parsed = parsed or parse_agent_receipt(text, tid)
        if parsed is None:
            tracking.finish_dispatch(dispatch_id, verdict="fail", receipt=text[:2000] or None)
            return CeremonyReceipt(task=tid, sha="uncommitted", passed=False,
                                   findings=[f"no BRIEF {tid} receipt line in the agent result"], tokens=tokens)
        tracking.finish_dispatch(dispatch_id, verdict="pass" if parsed.passed else "fail",
                                 receipt=text[:2000] or None)
        return CeremonyReceipt(task=tid, sha=parsed.sha, passed=parsed.passed, findings=parsed.findings,
                               tokens=tokens if tokens is not None else parsed.tokens)

    def correct(findings: list[str]) -> tuple[int, bool, Path, Path]:
        """Start a fresh same-task correction dispatch under the live lease."""
        if attempt >= max_attempts:
            abort(attempt, findings)
        if reset_window_every is not None and attempt % reset_window_every == 0:
            pool.mark_free(window)
            if pool.take_free(tid) != window:
                raise CeremonyError(f"Task {tid} could not renew its window")
        brief_file, receipt_file = write_brief(findings)
        record = tracking.renew_dispatch(tid, model=model, continuation=True,
                                         brief_digest=hashlib.sha256(brief_file.read_bytes()).hexdigest())
        pool.update(window, attempts=attempt, brief=str(brief_file))
        store_set(attempts=attempt, findings=findings, status="running")
        return record["dispatch_id"], True, brief_file, receipt_file

    brief_file, receipt_file = write_brief()
    try:
        record = tracking.dispatch(
            tid, session_id=session_id, owns=owns, branch=branch, worktree=worktree, model=model,
            continuation=False, brief_digest=hashlib.sha256(brief_file.read_bytes()).hexdigest())
    except LeaseError as error:
        raise CeremonyError(f"Task {tid} cannot dispatch: {error}") from error
    dispatch_id, continuation = record["dispatch_id"], False
    pool.mark_busy(window, tid, title=title, brief=str(brief_file), attempt=attempt)
    store_set(status="running", window=window)

    while True:
        receipt = run_dispatch(dispatch_id, continuation, brief_file, receipt_file)
        if receipt.tokens is not None:
            total_tokens += receipt.tokens
            saw_tokens = True

        unowned = [p for p in wt.dirty_paths(worktree) if not wt.path_owned(p, owns)]
        if unowned:
            wt.discard_changes(worktree)
            attempt += 1
            dispatch_id, continuation, brief_file, receipt_file = correct(
                [f"modified unowned path {', '.join(unowned)}"])
            continue

        if not receipt.passed:
            attempt += 1
            dispatch_id, continuation, brief_file, receipt_file = correct(
                list(receipt.findings) or [f"{tid} reported FAIL on attempt {attempt}"])
            continue

        committed = wt.commit_all(worktree, f"{tid}: implement attempt {attempt + 1}")
        head_sha = committed or wt.head(worktree, "HEAD") or ""
        if not _full_sha(head_sha):
            raise CeremonyError(f"Task {tid} produced no commit SHA for branch {branch}.")
        tracking.record_commit(tid, head_sha)

        try:
            handover = ci.handover(repo, task, base=base, verb="ready", tracking=tracking)
        except ci_mod.CIAdapterError as error:
            attempt += 1
            abort(attempt, [str(error)], reason="CI handover refused")
        pool.update(window, pr_number=handover.pr_number, head_sha=head_sha,
                    ci_status="submitted", attempts=attempt)
        store_set(pr_number=handover.pr_number, commit=head_sha, status="merging")

        try:
            evidence = ci.confirm_merge(repo, handover, tracking=tracking)
            if not _full_sha(evidence.get("squash_sha")):
                raise ci_mod.CIAdapterError("merge evidence has no verified squash commit")
        except ci_mod.CIAdapterError as error:
            findings = [f"CI did not confirm a verified squash merge: {error}"]
            try:
                tracking.record_ci(tid, pr_number=handover.pr_number, head_sha=head_sha,
                                   base_commit=handover.base_commit, service_reply={"error": str(error)},
                                   state="failed")
            except (LeaseError, ValueError):
                pass
            attempt += 1
            pool.update(window, ci_status="failed")
            dispatch_id, continuation, brief_file, receipt_file = correct(findings)
            continue

        merge_sha = evidence["squash_sha"]
        try:
            tracking.release(tid, reason="merged")
        except LeaseError as error:
            raise CeremonyError(
                f"Task {tid} merged at {merge_sha[:12]} but release refused, the lease stays live: {error}"
            ) from error
        pool.mark_free(window)
        tokens_val = total_tokens if saw_tokens else None
        ledger.record_done(
            task=tid, title=title, window=window, pr_number=handover.pr_number,
            head_sha=head_sha, merge_sha=merge_sha, attempts=attempt + 1,
            tokens=tokens_val, started_at=started_at, completed_at=now(),
        )
        if store is not None and store.read(f"/plan/tasks/{tid}") is not None:
            stages = dict(store.read(f"/plan/tasks/{tid}/stages") or {})
            stages["merge"] = {"status": "passed", "detail": "merged", "model": None}
            store_set(status="merged", commit=merge_sha, stages=stages)
        return TaskOutcome(
            task=tid, window=window, pr_number=handover.pr_number, head_sha=head_sha,
            merge_sha=merge_sha, attempts=attempt + 1, tokens=tokens_val, status="done",
        )


def main(argv: list[str] | None = None, *, runner: ci_mod.Runner = ci_mod.default_runner,
         harness_factory: Callable[..., Any] | None = None,
         ci: ci_mod.CIAdapter | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jbi-ceremony", description="Run the internal task dispatch lifecycle.")
    parser.add_argument("--dag", type=Path, required=True, help="path to task DAG (.json or restricted YAML)")
    parser.add_argument("--repo", type=Path, required=True, help="target git repository")
    parser.add_argument("--run-dir", type=Path, required=True, help="directory for briefs, receipts, pool and ledger")
    parser.add_argument("--windows", required=True, help="comma-separated window names for the pool")
    parser.add_argument("--base", default=wt.MAIN, help="base branch for draft PRs")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model name for the lite harness")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS,
                        help="maximum attempts per task before aborting")
    parser.add_argument("--reset-window-every", type=int, default=None,
                        help="start a fresh session every N failed attempts")
    args = parser.parse_args(argv)

    run_dir = args.run_dir.expanduser().resolve()
    repo = args.repo.expanduser().resolve()
    plan = dag_loader.load(args.dag.expanduser().resolve())
    windows = [w.strip() for w in args.windows.split(",") if w.strip()]
    pool = WindowPool(run_dir / "window_pool.json", windows=windows)
    ledger = TaskLedger(run_dir / "task_ledger.jsonl")
    run_ceremony(
        plan, pool=pool, ledger=ledger, repo=repo, run_dir=run_dir, base=args.base, model=args.model,
        runner=runner, ci=ci, harness_factory=harness_factory, max_attempts=args.max_attempts,
        reset_window_every=args.reset_window_every,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
