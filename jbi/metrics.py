"""Internal metrics: read the fleet store (`fleet.db`) and shape the payload for the metrics UI.

Every number comes from the `Tracking` store of the run. A value that the store does not
hold is None, never zero. When `fleet.db` does not exist, every section is an empty list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .tracking import Tracking

FLEET_DB = "fleet.db"
SECTIONS = ("sessions", "tasks", "dispatches", "leases", "ci_submissions", "merges", "messages", "costs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _empty(path: Path | str, error: str) -> dict[str, Any]:
    out: dict[str, Any] = {"available": False, "status": "unavailable", "path": str(path), "error": error}
    out.update({name: [] for name in SECTIONS})
    out.update(cache_read=None, cache_write=None, cache_hit_rate=None)
    return out


def read_tracking(run_dir: Path | str | None) -> dict[str, Any]:
    """Return the fleet of `run_dir/fleet.db`, closed again. A missing store is unavailable."""
    if run_dir is None:
        return _empty("", "no run directory")
    path = Path(run_dir) / FLEET_DB
    if not path.is_file():
        return _empty(path, "fleet store does not exist")
    tracking = None
    try:
        tracking = Tracking(path)
        fleet = tracking.fleet()
    except Exception as error:               # a damaged store must not break the metrics route
        return _empty(path, f"fleet store unreadable: {type(error).__name__}")
    finally:
        if tracking is not None:
            tracking.close()
    return {"available": True, "status": "ok", "path": str(path), "error": None, **fleet}


def _stamp(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _by_day(rows: list[dict], key: str) -> list[dict]:
    days: dict[str, int] = {}
    for row in rows:
        day = str(row.get(key) or "")[:10]
        if day:
            days[day] = days.get(day, 0) + 1
    return [{"date": d, "count": days[d]} for d in sorted(days)]


def _token_sum(cost: dict) -> int | None:
    parts = [cost.get("input_tokens"), cost.get("output_tokens")]
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


def summarize(fleet: dict[str, Any]) -> dict[str, Any]:
    """Derive the UI sections from one fleet read. Unknown stays None."""
    base = {"available": fleet["available"], "status": fleet["status"], "path": fleet["path"],
            "error": fleet["error"]}
    tasks = {t["id"]: t for t in fleet["tasks"]}
    dispatch_task = {d["id"]: d["task_id"] for d in fleet["dispatches"]}
    per_task: dict[str, int] = {}
    day_tokens: dict[str, int] = {}
    for cost in fleet["costs"]:
        total = _token_sum(cost)
        if total is None:
            continue
        tid = dispatch_task.get(cost["dispatch_id"])
        if tid is not None:
            per_task[tid] = per_task.get(tid, 0) + total
        day = str(cost.get("updated_at") or "")[:10]
        if day:
            day_tokens[day] = day_tokens.get(day, 0) + total
    durations, tokens = [], []
    for merge in fleet["merges"]:
        task, end = tasks.get(merge["task_id"]), _stamp(merge["merged_at"])
        start = _stamp(task["created_at"]) if task else None
        if start and end:
            durations.append((end - start).total_seconds())
        if merge["task_id"] in per_task:
            tokens.append(per_task[merge["task_id"]])
    token_total = sum(day_tokens.values()) if day_tokens else None
    return {
        "tokens": {**base, "total": token_total, "cache_read": fleet["cache_read"],
                   "cache_write": fleet["cache_write"],
                   "by_day": [{"date": d, "tokens": day_tokens[d]} for d in sorted(day_tokens)]},
        "task_ledger": {**base, "completed_count": len(fleet["merges"]) if fleet["available"] else None,
                        "avg_tokens_per_task": _mean(tokens), "avg_time_per_task_s": _mean(durations),
                        "tasks_per_day": _by_day(fleet["merges"], "merged_at")},
        "prs_merged": {**base, "total": len(fleet["merges"]) if fleet["available"] else None,
                       "by_day": _by_day(fleet["merges"], "merged_at")},
    }


def collect_metrics(run_dir: Path | str | None = None) -> dict[str, Any]:
    """Read the internal store and assemble the payload that the metrics route serves."""
    fleet = read_tracking(run_dir)
    summary = summarize(fleet)
    ledger, tokens = summary["task_ledger"], summary["tokens"]
    return {
        "generated_at": _now(),
        "sessions": fleet["sessions"], "tasks": fleet["tasks"], "dispatches": fleet["dispatches"],
        "leases": fleet["leases"], "ci_submissions": fleet["ci_submissions"], "merges": fleet["merges"],
        "messages": fleet["messages"], "costs": fleet["costs"],
        "cache": None if fleet["cache_hit_rate"] is None and fleet["cache_read"] is None else
                 {"read": fleet["cache_read"], "write": fleet["cache_write"], "hit_rate": fleet["cache_hit_rate"]},
        "store": {k: fleet[k] for k in ("available", "status", "path", "error")},
        **summary,
        "over_time": {"tokens_per_day": tokens["by_day"] if fleet["available"] else None,
                      "prs_merged_per_day": summary["prs_merged"]["by_day"] if fleet["available"] else None,
                      "tasks_completed_per_day": ledger["tasks_per_day"] if fleet["available"] else None},
        "averages": {"tokens_per_task": ledger["avg_tokens_per_task"],
                     "time_per_task_s": ledger["avg_time_per_task_s"]},
    }
