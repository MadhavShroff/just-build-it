"""Direct metrics reader for the dispatcher metrics UI.

Reads the six canonical sources directly with no LLM call:
1. Tokens used (total and per day): `~/.cache/glm-proxy/*.jsonl` and `~/.claude/projects/`
2. Tokens per task, time per task, and tasks completed per day: dispatcher `task_ledger.jsonl`
3. PRs merged per day: `git log origin/develop --since` in `/home/madhavshroff/tangram-workspace`
4. Train state, lanes, held greens: `~/.tangram-dev/ci-train/history.jsonl`
5. Board rows open and done: `/home/madhavshroff/.tangram-dev/board.db`
6. Window pool free and busy: dispatcher `window_pool.json`

When any source is missing or unreadable, its section returns `available: False`,
`status: "unavailable"`, `path: "<source path>"`, and `None` for every numeric metric
so the UI never invents a number.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_GLM_PROXY_DIR = Path.home() / ".cache" / "glm-proxy"
DEFAULT_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_WORKSPACE_REPO = Path("/home/madhavshroff/tangram-workspace")
DEFAULT_CI_TRAIN_HISTORY = Path.home() / ".tangram-dev" / "ci-train" / "history.jsonl"
DEFAULT_BOARD_DB = Path("/home/madhavshroff/.tangram-dev/board.db")

PR_SUBJECT_RE = re.compile(r"\(#(\d+)\)\s*$|Merge pull request #(\d+)\b")
TRAIN_LANE_RE = re.compile(r"-l(\d+)-")
RG_TOKEN_PATTERN = r'"timestamp":"\d{4}-\d{2}-\d{2}|"(?:input|output|cache_read_input|cache_creation_input)_tokens":\d+'
PY_TS_RE = re.compile(rb'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2})')
PY_TOKEN_RE = re.compile(rb'"(?:input|output|cache_read_input|cache_creation_input)_tokens"\s*:\s*(\d+)')

_CACHE_LOCK = threading.Lock()
# path -> (mtime_ns, size, total_tokens, {day: tokens})
_FILE_TOKEN_CACHE: dict[str, tuple[int, int, int, dict[str, int]]] = {}
# dir_path -> (dir_Check_key, total_tokens, {day: tokens})
_DIR_TOKEN_CACHE: dict[str, tuple[tuple[int, int, int], int, dict[str, int]]] = {}


@dataclass(frozen=True)
class MetricsSources:
    glm_proxy_dir: Path = DEFAULT_GLM_PROXY_DIR
    claude_projects_dir: Path = DEFAULT_CLAUDE_PROJECTS_DIR
    task_ledger_path: Path | None = None
    workspace_repo: Path = DEFAULT_WORKSPACE_REPO
    ci_train_history: Path = DEFAULT_CI_TRAIN_HISTORY
    board_db: Path = DEFAULT_BOARD_DB
    window_registry_path: Path | None = None
    days: int = 14


def _unavailable(path: Path | str, error: str, **null_fields: Any) -> dict[str, Any]:
    return {
        "available": False,
        "status": "unavailable",
        "path": str(path),
        "error": error,
        **null_fields,
    }


def _day_from_ts(value: Any, fallback_mtime: float | None = None) -> str | None:
    if isinstance(value, str) and len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
        return value[:10]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if ts > 1e11:  # epoch milliseconds
            ts /= 1000.0
        if ts > 0:
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            except (OSError, OverflowError, ValueError):
                pass
    if fallback_mtime is not None:
        try:
            return datetime.fromtimestamp(fallback_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            pass
    return None


def _tokens_from_dict(obj: dict[str, Any]) -> int | None:
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        msg = obj.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
            usage = msg["usage"]
    target = usage if isinstance(usage, dict) else obj
    if isinstance(target.get("total_tokens"), int) and not isinstance(target.get("total_tokens"), bool):
        return int(target["total_tokens"])
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "prompt_tokens",
        "completion_tokens",
    )
    vals = [
        int(target[k])
        for k in keys
        if isinstance(target.get(k), (int, float)) and not isinstance(target.get(k), bool)
    ]
    return sum(vals) if vals else None


def _scan_jsonl_file(path: Path) -> tuple[int, dict[str, int]]:
    stat = path.stat()
    key = str(path)
    with _CACHE_LOCK:
        cached = _FILE_TOKEN_CACHE.get(key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2], dict(cached[3])

    total = 0
    by_day: dict[str, int] = {}
    fallback_day = _day_from_ts(None, stat.st_mtime)

    if stat.st_size <= 4 * 1024 * 1024:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or ("token" not in line and "usage" not in line):
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                tok = _tokens_from_dict(data)
                if tok is None:
                    continue
                day = _day_from_ts(data.get("timestamp") or data.get("ts") or data.get("date"), stat.st_mtime) or fallback_day
                total += tok
                if day:
                    by_day[day] = by_day.get(day, 0) + tok
    else:
        with path.open("rb") as handle:
            for raw_b in handle:
                if b"_tokens" not in raw_b:
                    continue
                tok = sum(int(m.group(1)) for m in PY_TOKEN_RE.finditer(raw_b))
                if not tok:
                    continue
                ts_m = PY_TS_RE.search(raw_b)
                day = ts_m.group(1).decode("ascii") if ts_m else fallback_day
                total += tok
                if day:
                    by_day[day] = by_day.get(day, 0) + tok

    with _CACHE_LOCK:
        _FILE_TOKEN_CACHE[key] = (stat.st_mtime_ns, stat.st_size, total, dict(by_day))
    return total, by_day


def read_glm_proxy_tokens(glm_dir: Path) -> dict[str, Any]:
    pattern_display = f"{glm_dir}/*.jsonl"
    if not glm_dir.exists() or not glm_dir.is_dir():
        return _unavailable(pattern_display, "directory does not exist", total=None, by_day={})
    try:
        files = sorted(p for p in glm_dir.glob("*.jsonl") if p.is_file())
    except OSError as error:
        return _unavailable(pattern_display, str(error), total=None, by_day={})
    if not files:
        return _unavailable(pattern_display, "no .jsonl files found", total=None, by_day={})

    total = 0
    by_day: dict[str, int] = {}
    readable = 0
    for path in files:
        try:
            f_total, f_days = _scan_jsonl_file(path)
            readable += 1
            total += f_total
            for day, count in f_days.items():
                by_day[day] = by_day.get(day, 0) + count
        except OSError:
            continue
    if readable == 0:
        return _unavailable(pattern_display, "no readable .jsonl files", total=None, by_day={})
    return {
        "available": True,
        "status": "ok",
        "path": pattern_display,
        "error": None,
        "total": total,
        "by_day": by_day,
    }


def _find_rg() -> str | None:
    cargo_rg = Path.home() / ".cargo" / "bin" / "rg"
    if cargo_rg.is_file() and os.access(cargo_rg, os.X_OK):
        return str(cargo_rg)
    return shutil.which("rg")


def read_claude_tokens(claude_dir: Path) -> dict[str, Any]:
    if not claude_dir.exists() or not claude_dir.is_dir():
        return _unavailable(claude_dir, "directory does not exist", total=None, by_day={})
    try:
        files = [p for p in claude_dir.rglob("*.jsonl") if p.is_file()]
    except OSError as error:
        return _unavailable(claude_dir, str(error), total=None, by_day={})
    if not files:
        return _unavailable(claude_dir, "no transcript .jsonl files found", total=None, by_day={})

    try:
        stats = [(p, p.stat()) for p in files]
    except OSError as error:
        return _unavailable(claude_dir, str(error), total=None, by_day={})

    total_bytes = sum(st.st_size for _, st in stats)
    max_mtime = max((st.st_mtime_ns for _, st in stats), default=0)
    dir_sig = (len(stats), total_bytes, max_mtime)
    dir_key = str(claude_dir.resolve())

    with _CACHE_LOCK:
        cached_dir = _DIR_TOKEN_CACHE.get(dir_key)
        if cached_dir and cached_dir[0] == dir_sig:
            return {
                "available": True,
                "status": "ok",
                "path": str(claude_dir),
                "error": None,
                "total": cached_dir[1],
                "by_day": dict(cached_dir[2]),
            }

    rg_bin = _find_rg()
    if total_bytes > 32 * 1024 * 1024 and rg_bin is not None:
        res = subprocess.run(
            [
                rg_bin,
                "--no-heading",
                "--with-filename",
                "--no-line-number",
                "--field-match-separator=\t",
                "-o",
                RG_TOKEN_PATTERN,
                str(claude_dir),
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        if res.returncode in (0, 1):
            total = 0
            by_day: dict[str, int] = {}
            current_day: dict[str, str] = {}
            for line in res.stdout.splitlines():
                fn, sep, match = line.partition("\t")
                if not sep:
                    continue
                if match.startswith('"timestamp":"'):
                    current_day[fn] = match[-10:]
                else:
                    _, _, num_str = match.rpartition(":")
                    if num_str.isdigit():
                        val = int(num_str)
                        total += val
                        day = current_day.get(fn)
                        if day:
                            by_day[day] = by_day.get(day, 0) + val
            with _CACHE_LOCK:
                _DIR_TOKEN_CACHE[dir_key] = (dir_sig, total, dict(by_day))
            return {
                "available": True,
                "status": "ok",
                "path": str(claude_dir),
                "error": None,
                "total": total,
                "by_day": by_day,
            }

    total = 0
    by_day = {}
    readable = 0
    for path, _ in stats:
        try:
            f_total, f_days = _scan_jsonl_file(path)
            readable += 1
            total += f_total
            for day, count in f_days.items():
                by_day[day] = by_day.get(day, 0) + count
        except OSError:
            continue
    if readable == 0:
        return _unavailable(claude_dir, "no readable transcript files", total=None, by_day={})
    with _CACHE_LOCK:
        _DIR_TOKEN_CACHE[dir_key] = (dir_sig, total, dict(by_day))
    return {
        "available": True,
        "status": "ok",
        "path": str(claude_dir),
        "error": None,
        "total": total,
        "by_day": by_day,
    }


def read_tokens(glm_dir: Path, claude_dir: Path) -> dict[str, Any]:
    glm = read_glm_proxy_tokens(glm_dir)
    claude = read_claude_tokens(claude_dir)
    combined_path = f"{glm['path']}, {claude['path']}"
    if not glm["available"] and not claude["available"]:
        return _unavailable(
            combined_path,
            f"glm-proxy ({glm['error']}); claude ({claude['error']})",
            total=None,
            by_day=[],
            sources={"glm_proxy": glm, "claude_projects": claude},
        )
    total = (glm["total"] or 0) + (claude["total"] or 0)
    merged_days: dict[str, int] = {}
    for src in (glm, claude):
        if src["available"]:
            for day, val in src["by_day"].items():
                merged_days[day] = merged_days.get(day, 0) + val
    series = [{"date": d, "tokens": merged_days[d]} for d in sorted(merged_days)]
    return {
        "available": True,
        "status": "ok",
        "path": combined_path,
        "error": None,
        "total": total,
        "by_day": series,
        "sources": {
            "glm_proxy": {
                **glm,
                "by_day": [{"date": d, "tokens": v} for d, v in sorted((glm.get("by_day") or {}).items())],
            },
            "claude_projects": {
                **claude,
                "by_day": [{"date": d, "tokens": v} for d, v in sorted((claude.get("by_day") or {}).items())],
            },
        },
    }


def read_task_ledger(ledger_path: Path) -> dict[str, Any]:
    if not ledger_path.exists() or not ledger_path.is_file():
        return _unavailable(
            ledger_path,
            "task ledger file does not exist",
            completed_count=None,
            avg_tokens_per_task=None,
            avg_time_per_task_s=None,
            tasks_per_day=[],
            recent_tasks=[],
        )
    try:
        raw_lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return _unavailable(
            ledger_path,
            str(error),
            completed_count=None,
            avg_tokens_per_task=None,
            avg_time_per_task_s=None,
            tasks_per_day=[],
            recent_tasks=[],
        )

    entries: list[dict[str, Any]] = []
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)

    done = [e for e in entries if e.get("status", "done") in ("done", "merged")]
    token_values = [
        int(e["tokens"])
        for e in done
        if isinstance(e.get("tokens"), (int, float)) and not isinstance(e.get("tokens"), bool)
    ]
    duration_values = [
        float(e["duration_s"])
        for e in done
        if isinstance(e.get("duration_s"), (int, float)) and not isinstance(e.get("duration_s"), bool)
    ]
    by_day: dict[str, int] = {}
    for entry in done:
        day = _day_from_ts(entry.get("completed_at") or entry.get("ts") or entry.get("started_at"))
        if day:
            by_day[day] = by_day.get(day, 0) + 1

    avg_tokens = round(sum(token_values) / len(token_values), 1) if token_values else None
    avg_time = round(sum(duration_values) / len(duration_values), 2) if duration_values else None
    return {
        "available": True,
        "status": "ok",
        "path": str(ledger_path),
        "error": None,
        "completed_count": len(done),
        "avg_tokens_per_task": avg_tokens,
        "avg_time_per_task_s": avg_time,
        "tasks_per_day": [{"date": d, "count": by_day[d]} for d in sorted(by_day)],
        "recent_tasks": done[-20:],
    }


def read_prs_merged(workspace_repo: Path, *, days: int = 14) -> dict[str, Any]:
    if not workspace_repo.exists() or not (workspace_repo / ".git").exists():
        return _unavailable(
            workspace_repo,
            "git repository does not exist",
            total=None,
            by_day=[],
            recent=[],
        )
    res = subprocess.run(
        [
            "git",
            "-C",
            str(workspace_repo),
            "log",
            "origin/develop",
            f"--since={days}.days.ago",
            "--date=short",
            "--pretty=format:%ad\t%H\t%s",
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "git log origin/develop failed").strip()
        return _unavailable(
            workspace_repo,
            detail,
            total=None,
            by_day=[],
            recent=[],
        )

    by_day: dict[str, int] = {}
    recent: list[dict[str, Any]] = []
    for line in res.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        day, sha, subject = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not day:
            continue
        match = PR_SUBJECT_RE.search(subject)
        pr_num = int(match.group(1) or match.group(2)) if match else None
        by_day[day] = by_day.get(day, 0) + 1
        if len(recent) < 25:
            recent.append({"date": day, "sha": sha[:10], "pr_number": pr_num, "subject": subject})

    series = [{"date": d, "count": by_day[d]} for d in sorted(by_day)]
    return {
        "available": True,
        "status": "ok",
        "path": str(workspace_repo),
        "error": None,
        "total": sum(by_day.values()),
        "by_day": series,
        "recent": recent,
    }


def read_ci_train(history_path: Path) -> dict[str, Any]:
    if not history_path.exists() or not history_path.is_file():
        return _unavailable(
            history_path,
            "history.jsonl does not exist",
            train_state=None,
            train_id=None,
            lanes=[],
            held_greens=[],
            prs_in_ci=[],
        )
    try:
        raw_lines = history_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return _unavailable(
            history_path,
            str(error),
            train_state=None,
            train_id=None,
            lanes=[],
            held_greens=[],
            prs_in_ci=[],
        )

    records: list[dict[str, Any]] = []
    for raw in raw_lines[-500:]:
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            records.append(item)

    if not records:
        return _unavailable(
            history_path,
            "history.jsonl contains no valid JSON records",
            train_state=None,
            train_id=None,
            lanes=[],
            held_greens=[],
            prs_in_ci=[],
        )

    lanes_map: dict[str, dict[str, Any]] = {}
    held_map: dict[str, dict[str, Any]] = {}
    explicit_prs: list[dict[str, Any]] = []
    train_state = "idle"
    latest_train_id: str | None = None

    for rec in records:
        tid = str(rec.get("train_id") or "") or None
        if tid:
            latest_train_id = tid

        if isinstance(rec.get("lanes"), list):
            for lane_obj in rec["lanes"]:
                if isinstance(lane_obj, dict):
                    lid = str(lane_obj.get("lane") or lane_obj.get("id") or len(lanes_map) + 1)
                    lanes_map[lid] = {
                        "lane": lid,
                        "train_id": lane_obj.get("train_id") or tid,
                        "status": str(lane_obj.get("status") or lane_obj.get("state") or "idle"),
                        "prs": list(lane_obj.get("prs") or []),
                        "head_sha": lane_obj.get("head_sha"),
                    }

        if isinstance(rec.get("held_greens"), list):
            held_map = {
                str( i): (item if isinstance(item, dict) else {"train_id": str(item)})
                for i, item in enumerate(rec["held_greens"])
            }

        if isinstance(rec.get("prs_in_ci"), list):
            explicit_prs = [
                (p if isinstance(p, dict) else {"pr_number": p})
                for p in rec["prs_in_ci"]
            ]

        if rec.get("record") == "merge" and isinstance(rec.get("merge"), dict):
            merge_info = rec["merge"]
            input_key = str(rec.get("input_key") or tid or "")
            m_state = merge_info.get("state")
            if m_state == "green-waiting-other-lanes" or merge_info.get("stopped") == "merge-serialized":
                held_map[input_key] = {
                    "train_id": tid,
                    "input_key": input_key,
                    "state": m_state or "held-green",
                    "other_lanes": list(merge_info.get("other_lanes") or []),
                    "stamped": rec.get("stamped"),
                }
                train_state = str(m_state or "green-waiting-other-lanes")
            elif merge_info.get("attempted") or merge_info.get("merged"):
                held_map.pop(input_key, None)
                train_state = "merged" if merge_info.get("merged") else str(m_state or "idle")
            elif m_state:
                train_state = str(m_state)
        else:
            lane_id = rec.get("lane")
            if lane_id is None and tid:
                m_lane = TRAIN_LANE_RE.search(tid)
                if m_lane:
                    lane_id = int(m_lane.group(1))
            verdict = str(rec.get("verdict") or rec.get("state") or rec.get("payload_outcome") or (
                "green" if rec.get("exit_code") == 0 else ("red" if rec.get("exit_code") else "running")
            ))
            if lane_id is not None:
                lid = str(lane_id)
                lanes_map[lid] = {
                    "lane": lid,
                    "train_id": tid,
                    "status": verdict,
                    "prs": list(rec.get("prs") or []),
                    "head_sha": rec.get("head_sha"),
                    "failed_gates": list(rec.get("failed_gates") or []),
                    "finished": rec.get("finished") or rec.get("started"),
                }
            if rec.get("state") or rec.get("verdict"):
                train_state = verdict

    lanes_list = [lanes_map[k] for k in sorted(lanes_map, key=lambda x: (len(x), x))]
    held_list = list(held_map.values())
    prs_in_ci = list(explicit_prs)
    if not prs_in_ci:
        for lane in lanes_list:
            if lane.get("status") in ("running", "testing", "queued", "green-waiting-other-lanes"):
                for pr in lane.get("prs") or []:
                    prs_in_ci.append({
                        "pr_number": pr,
                        "lane": lane["lane"],
                        "train_id": lane.get("train_id"),
                        "status": lane.get("status"),
                    })

    return {
        "available": True,
        "status": "ok",
        "path": str(history_path),
        "error": None,
        "train_state": train_state,
        "train_id": latest_train_id,
        "lanes": lanes_list,
        "held_greens": held_list,
        "prs_in_ci": prs_in_ci,
    }


def read_board_db(board_db: Path) -> dict[str, Any]:
    if not board_db.exists() or not board_db.is_file():
        return _unavailable(board_db, "board.db does not exist", open_rows=None, done_rows=None, by_state={})
    try:
        with sqlite3.connect(f"file:{board_db}?mode=ro", uri=True) as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "todo_items" in tables:
                rows = conn.execute("SELECT state, COUNT(*) FROM todo_items GROUP BY state").fetchall()
            elif "todos" in tables:
                rows = conn.execute("SELECT status, COUNT(*) FROM todos GROUP BY status").fetchall()
            else:
                return _unavailable(
                    board_db,
                    "board.db has no todo_items table",
                    open_rows=None,
                    done_rows=None,
                    by_state={},
                )
    except sqlite3.Error as error:
        return _unavailable(board_db, str(error), open_rows=None, done_rows=None, by_state={})

    by_state = {str(state or "unknown"): int(count) for state, count in rows}
    done_rows = by_state.get("done", 0)
    open_rows = sum(
        count for state, count in by_state.items() if state not in ("done", "dropped")
    )
    return {
        "available": True,
        "status": "ok",
        "path": str(board_db),
        "error": None,
        "open_rows": open_rows,
        "done_rows": done_rows,
        "by_state": by_state,
    }


def read_window_registry(registry_path: Path) -> dict[str, Any]:
    if not registry_path.exists() or not registry_path.is_file():
        return _unavailable(
            registry_path,
            "window registry does not exist",
            free_count=None,
            busy_count=None,
            windows=[],
            busy_windows=[],
            prs_in_ci=[],
        )
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return _unavailable(
            registry_path,
            str(error),
            free_count=None,
            busy_count=None,
            windows=[],
            busy_windows=[],
            prs_in_ci=[],
        )
    if not isinstance(raw, dict) or not isinstance(raw.get("windows"), dict):
        return _unavailable(
            registry_path,
            "window registry JSON is missing 'windows' object",
            free_count=None,
            busy_count=None,
            windows=[],
            busy_windows=[],
            prs_in_ci=[],
        )

    windows: list[dict[str, Any]] = []
    busy_windows: list[dict[str, Any]] = []
    prs_in_ci: list[dict[str, Any]] = []
    free_count = 0
    for name in sorted(raw["windows"]):
        entry = raw["windows"][name]
        if not isinstance(entry, dict):
            continue
        status = "busy" if entry.get("status") == "busy" else "free"
        item = {
            "window": str(name),
            "status": status,
            "task": entry.get("task") if status == "busy" else None,
            "title": entry.get("title") if status == "busy" else None,
            "pr_number": entry.get("pr_number") if status == "busy" else None,
            "head_sha": entry.get("head_sha") if status == "busy" else None,
            "ci_status": entry.get("ci_status") if status == "busy" else None,
            "attempts": int(entry.get("attempts") or 0) if status == "busy" else 0,
            "started_at": entry.get("started_at") if status == "busy" else None,
            "updated_at": entry.get("updated_at"),
        }
        windows.append(item)
        if status == "busy":
            busy_windows.append(item)
            if item["pr_number"] is not None:
                prs_in_ci.append({
                    "pr_number": item["pr_number"],
                    "task": item["task"],
                    "window": item["window"],
                    "head_sha": item["head_sha"],
                    "status": item["ci_status"] or "submitted",
                })
        else:
            free_count += 1

    return {
        "available": True,
        "status": "ok",
        "path": str(registry_path),
        "error": None,
        "free_count": free_count,
        "busy_count": len(busy_windows),
        "windows": windows,
        "busy_windows": busy_windows,
        "prs_in_ci": prs_in_ci,
    }


def collect_metrics(sources: MetricsSources | None = None, *, run_dir: Path | None = None) -> dict[str, Any]:
    """Read all six metrics sources directly and assemble the deterministic UI payload."""
    cfg = sources or MetricsSources()
    base_dir = Path(run_dir) if run_dir else (Path.home() / ".tangram-dev" / "dev-dispatcher")
    ledger_path = Path(cfg.task_ledger_path) if cfg.task_ledger_path else (base_dir / "task_ledger.jsonl")
    registry_path = Path(cfg.window_registry_path) if cfg.window_registry_path else (base_dir / "window_pool.json")

    tokens = read_tokens(Path(cfg.glm_proxy_dir), Path(cfg.claude_projects_dir))
    task_ledger = read_task_ledger(ledger_path)
    prs_merged = read_prs_merged(Path(cfg.workspace_repo), days=cfg.days)
    ci_train = read_ci_train(Path(cfg.ci_train_history))
    board = read_board_db(Path(cfg.board_db))
    window_pool = read_window_registry(registry_path)

    active_prs: list[dict[str, Any]] = []
    seen_prs: set[Any] = set()
    for pr_item in [*(window_pool.get("prs_in_ci") or []), *(ci_train.get("prs_in_ci") or [])]:
        key = pr_item.get("pr_number")
        if key is not None and key in seen_prs:
            continue
        if key is not None:
            seen_prs.add(key)
        active_prs.append(pr_item)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "tokens": tokens,
        "task_ledger": task_ledger,
        "prs_merged": prs_merged,
        "ci_train": ci_train,
        "board": board,
        "window_pool": window_pool,
        "right_now": {
            "busy_windows": window_pool.get("busy_windows") if window_pool["available"] else None,
            "prs_in_ci": active_prs if (window_pool["available"] or ci_train["available"]) else None,
        },
        "over_time": {
            "tokens_per_day": tokens.get("by_day") if tokens["available"] else None,
            "prs_merged_per_day": prs_merged.get("by_day") if prs_merged["available"] else None,
            "tasks_completed_per_day": task_ledger.get("tasks_per_day") if task_ledger["available"] else None,
        },
        "averages": {
            "tokens_per_task": task_ledger.get("avg_tokens_per_task"),
            "time_per_task_s": task_ledger.get("avg_time_per_task_s"),
        },
    }
