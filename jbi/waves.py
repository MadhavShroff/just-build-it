"""Plan validation and wave compilation.

A plan is an ordered list of task dicts. `compile_plan` validates the list, computes
Kahn topological levels in plan order, and returns the `plan` section of the state
document. A wave groups tasks for display only: the scheduler starts a task as soon
as its own dependencies are merged (`ready_tasks`).
"""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path
from typing import Any, Iterable

from . import rigor
from .protocol import TASK_ID, ProtocolError

REQUIRED = ("id", "title", "goal", "deps", "acceptance")
DONE = "merged"

STAGE_ETA_SECONDS: dict[str, int] = {
    "implement": 180,
    "acceptance": 30,
    "verify": 60,
    "review": 45,
    "security": 45,
    "merge": 60,
}
OWNS_FILE_ETA_SECONDS = 15
EFFORT_ETA_SECONDS: dict[str, int] = {
    "XS": 180,
    "S": 300,
    "M": 600,
    "L": 900,
    "XL": 1200,
}
OPTIONAL_TASK_META = (
    "node", "batch", "owner", "path", "spec", "deliverable",
    "effort", "eta_s", "estimated_seconds", "consumers", "prTag", "prTitle", "number",
    "upstream_deps", "spec_details", "contracts", "invariants", "test_cases", "done_criteria",
)


def normalize(task: dict) -> dict:
    """Return a task with every field the state document expects."""
    out = {
        "id": task["id"], "title": task["title"].strip(), "goal": task["goal"].strip(),
        "deps": list(task.get("deps", [])), "acceptance": task["acceptance"].strip(),
        "owns": list(task.get("owns", [])), "components": list(task.get("components", [])),
        "wave": 0, "status": "blocked", "stage": None, "attempts": 0,
        # A DAG node's routing override must survive compilation. The supervisor
        # reads the compiled plan, so an override dropped here is an override the
        # runtime never sees: a pinned node would run on the cascade instead.
        "override": dict(task.get("override") or {}),
        "branch": f"jbi/{task['id']}", "commit": None, "receipt": None,
    }
    for key in OPTIONAL_TASK_META:
        if key in task and task[key] is not None:
            out[key] = task[key]
    return out


def validate(tasks: list[dict]) -> None:
    if not tasks:
        raise ProtocolError("conflict", "The plan has no tasks.")
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        missing = [f for f in REQUIRED if f not in task]
        if missing:
            raise ProtocolError("conflict", f"Task {index + 1} lacks {', '.join(missing)}.", task=index)
        tid = task["id"]
        if not isinstance(tid, str) or not TASK_ID.match(tid):
            raise ProtocolError("conflict", f"Task id {tid!r} must match {TASK_ID.pattern}.", task=tid)
        if tid in seen:
            raise ProtocolError("conflict", f"Task id {tid!r} appears twice.", task=tid)
        seen.add(tid)
        for name in ("title", "goal", "acceptance"):
            if not isinstance(task[name], str) or not task[name].strip():
                raise ProtocolError("conflict", f"Task {tid!r} needs a non-empty {name}.", task=tid)
        for path in task.get("owns", []):
            if not isinstance(path, str) or path.startswith("/") or ".." in path.split("/") or "\\" in path:
                raise ProtocolError("conflict", f"Task {tid!r} owns an unsafe path {path!r}.", task=tid)
    for task in tasks:
        deps = task["deps"]
        if not isinstance(deps, list) or len(set(deps)) != len(deps):
            raise ProtocolError("conflict", f"Task {task['id']!r} has a bad or repeated dependency list.")
        for dep in deps:
            if dep == task["id"]:
                raise ProtocolError("conflict", f"Task {dep!r} depends on itself.", cycle=[dep, dep])
            if dep not in seen:
                raise ProtocolError("conflict", f"Task {task['id']!r} depends on unknown task {dep!r}.")
    cycle = find_cycle({t["id"]: t["deps"] for t in tasks})
    if cycle:
        raise ProtocolError("conflict", "Dependency cycle: " + " -> ".join(cycle), cycle=cycle)


def find_cycle(deps: dict[str, list[str]]) -> list[str] | None:
    """Return one closed cycle such as [a, b, a], or None. Iterative, so deep plans are safe."""
    colour = {node: 0 for node in deps}          # 0 new, 1 on stack, 2 done
    for root in deps:
        if colour[root]:
            continue
        stack, path = [(root, iter(deps[root]))], [root]
        colour[root] = 1
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                colour[node] = 2
                stack.pop()
                path.pop()
            elif colour[child] == 1:
                return path[path.index(child):] + [child]
            elif colour[child] == 0:
                colour[child] = 1
                stack.append((child, iter(deps[child])))
                path.append(child)
    return None


def levels(tasks: list[dict]) -> list[list[str]]:
    """Kahn topological levels, each level in plan order."""
    order = [t["id"] for t in tasks]
    pending = {t["id"]: set(t["deps"]) for t in tasks}
    result: list[list[str]] = []
    done: set[str] = set()
    while len(done) < len(order):
        level = [tid for tid in order if tid not in done and pending[tid] <= done]
        if not level:
            raise ProtocolError("conflict", "The plan has a cycle.")
        result.append(level)
        done.update(level)
    return result


def compile_plan(tasks: list[dict] | dict, revision: int = 1) -> dict:
    explicit_waves: list[list[str]] | None = None
    if isinstance(tasks, dict):
        if isinstance(tasks.get("wave_groups"), list) and tasks.get("waves") == "explicit":
            explicit_waves = [list(g) for g in tasks["wave_groups"]]
        raw_tasks = tasks.get("tasks")
        if isinstance(raw_tasks, dict):
            order = tasks.get("order") or list(raw_tasks)
            tasks = [raw_tasks[tid] for tid in order]
        elif isinstance(raw_tasks, list):
            tasks = raw_tasks
        else:
            tasks = []
    validate(tasks)
    normalized = {t["id"]: normalize(t) for t in tasks}
    waves = explicit_waves if explicit_waves is not None else levels(tasks)
    for number, wave in enumerate(waves, start=1):
        for tid in wave:
            normalized[tid]["wave"] = number
    for task in normalized.values():
        task["status"] = "ready" if not task["deps"] else "blocked"
    return {"revision": revision, "tasks": normalized, "order": [t["id"] for t in tasks], "waves": waves}


def ready_tasks(plan: dict) -> list[str]:
    """Tasks whose dependencies are all merged and that have not started.

    `waiting-model` is eligible again: a task that waits for a pinned model is
    not started and not failed, so the next pump must offer it to the scheduler
    again. The supervisor re-checks the pin each time, so a task that is still
    blocked simply waits again.
    """
    tasks = plan.get("tasks", {})
    return [tid for tid in plan.get("order", [])
            if tasks[tid]["status"] in ("blocked", "ready", "waiting-model")
            and all(tasks[d]["status"] == DONE for d in tasks[tid]["deps"])]


def _norm_own(path: str) -> str:
    p = path.strip()
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/") or p


def _is_universal_own(path: str) -> bool:
    return _norm_own(path) in ("*", "**", "**/*")


def _has_glob(segment: str) -> bool:
    return any(ch in segment for ch in ("*", "?", "["))


def _segments_overlap(a: str, b: str) -> bool:
    if a == b or a == "*" or b == "*":
        return True
    if not _has_glob(a) and not _has_glob(b):
        return False
    if not _has_glob(a):
        return fnmatch.fnmatchcase(a, b)
    if not _has_glob(b):
        return fnmatch.fnmatchcase(b, a)
    if "[" not in a and "[" not in b and "?" not in a and "?" not in b:
        pa, sa = a.split("*", 1)[0], a.rsplit("*", 1)[-1]
        pb, sb = b.split("*", 1)[0], b.rsplit("*", 1)[-1]
        return (pa.startswith(pb) or pb.startswith(pa)) and (sa.endswith(sb) or sb.endswith(sa))
    return True


def _can_be_directory(segments: list[str], raw: str) -> bool:
    if raw.endswith("/"):
        return True
    last = segments[-1]
    if last in ("*", "**"):
        return True
    if _has_glob(last):
        suffix = last.rsplit("*", 1)[-1]
        return "." not in suffix
    return "." not in last


def _paths_overlap(raw_a: str, raw_b: str) -> bool:
    a = _norm_own(raw_a)
    b = _norm_own(raw_b)
    if not a or not b:
        return False
    if a == b or _is_universal_own(a) or _is_universal_own(b):
        return True
    if not _has_glob(a) and not _has_glob(b):
        return a.startswith(b + "/") or b.startswith(a + "/")

    segs_a = [s for s in a.split("/") if s]
    segs_b = [s for s in b.split("/") if s]
    dir_a = _can_be_directory(segs_a, raw_a.strip())
    dir_b = _can_be_directory(segs_b, raw_b.strip())
    memo: dict[tuple[int, int], bool] = {}

    def match(i: int, j: int) -> bool:
        state = (i, j)
        if state in memo:
            return memo[state]
        if i == len(segs_a) or j == len(segs_b):
            res = True
        elif segs_a[i] == "**" and segs_b[j] == "**":
            res = match(i + 1, j) or match(i, j + 1) or match(i + 1, j + 1)
        elif segs_a[i] == "**":
            can_consume_b = (i + 1 == len(segs_a)) or (j + 1 < len(segs_b)) or dir_b
            res = match(i + 1, j) or (can_consume_b and match(i, j + 1))
        elif segs_b[j] == "**":
            can_consume_a = (j + 1 == len(segs_b)) or (i + 1 < len(segs_a)) or dir_a
            res = match(i, j + 1) or (can_consume_a and match(i + 1, j))
        elif _segments_overlap(segs_a[i], segs_b[j]):
            res = match(i + 1, j + 1)
        else:
            res = False
        memo[state] = res
        return res

    return match(0, 0)


def _owns_overlap(mine_raw: Iterable[str], theirs_raw: Iterable[str]) -> bool:
    mine = [p for p in mine_raw if isinstance(p, str) and p.strip()]
    theirs = [p for p in theirs_raw if isinstance(p, str) and p.strip()]
    if any(_is_universal_own(p) for p in mine) or any(_is_universal_own(p) for p in theirs):
        return True
    if not mine or not theirs:
        return False
    return any(_paths_overlap(a, b) for a in mine for b in theirs)


def owns_conflict(plan: dict, tid: str, running: Iterable[str]) -> str | None:
    """Return a running task that owns a path overlapping `tid`, or None."""
    mine = plan["tasks"][tid].get("owns") or []
    for other in running:
        if other != tid and _owns_overlap(mine, plan["tasks"][other].get("owns") or []):
            return other
    return None


def detect_wave_owns_conflicts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every pair of parallel tasks in the same wave whose `owns` paths overlap."""
    tasks = plan.get("tasks") or {}
    conflicts: list[dict[str, Any]] = []
    for wave_num, wave_ids in enumerate(plan.get("waves") or (), start=1):
        valid = [tid for tid in wave_ids if tid in tasks]
        for i, a in enumerate(valid):
            owns_a = tasks[a].get("owns") or ()
            for b in valid[i + 1:]:
                owns_b = tasks[b].get("owns") or ()
                if _owns_overlap(owns_a, owns_b):
                    conflicts.append({"wave": wave_num, "task_a": a, "task_b": b})
    return conflicts


def prioritize_ready(plan: dict[str, Any], ready_ids: Iterable[str] | None = None,
                     config: dict[str, Any] | None = None) -> list[str]:
    """Order ready tasks by finish-first boost, critical path, downstream fan-out, then plan order."""
    tasks = plan.get("tasks") or {}
    order = plan.get("order") or list(tasks)
    index_map = {tid: idx for idx, tid in enumerate(order)}
    candidates = list(ready_tasks(plan) if ready_ids is None else ready_ids)
    if len(candidates) <= 1:
        return candidates
    cp = set(critical_path(plan, config=config).get("path") or ())
    fanout: dict[str, int] = {tid: 0 for tid in tasks}
    for tid, task in tasks.items():
        if task.get("status") in (DONE, "skipped"):
            continue
        for dep in task.get("deps") or ():
            if dep in fanout:
                fanout[dep] += 1

    def sort_key(tid: str) -> tuple[int, int, int, int]:
        task = tasks.get(tid) or {}
        finish_boost = 0 if int(task.get("attempts") or 0) > 0 else 1
        on_cp = 0 if tid in cp else 1
        return (finish_boost, on_cp, -fanout.get(tid, 0), index_map.get(tid, 0))

    return sorted(candidates, key=sort_key)


def wave_preflight(plan: dict[str, Any], wave_number: int, *,
                   open_decisions: Iterable[dict[str, Any]] = (),
                   repo: Path | None = None, base_branch: str | None = None,
                   required_tools: Iterable[str] = ()) -> dict[str, Any]:
    """Deterministic Wave Preflight Gate before starting Wave `wave_number`."""
    tasks = plan.get("tasks") or {}
    raw_waves = plan.get("waves") or []
    blockers: list[str] = []
    warnings: list[str] = []
    if wave_number < 1 or wave_number > len(raw_waves):
        blockers.append(f"wave {wave_number} does not exist (plan has {len(raw_waves)} wave(s))")
        return {"ok": False, "wave": wave_number, "blockers": blockers, "warnings": warnings}

    wave_ids = [tid for tid in raw_waves[wave_number - 1] if tid in tasks]
    wave_set = set(wave_ids)
    wave_nodes = {str(tasks[tid].get("node") or tid) for tid in wave_ids}

    for tid in wave_ids:
        for dep in tasks[tid].get("deps") or ():
            if dep not in wave_set and dep in tasks and tasks[dep].get("status") != DONE:
                blockers.append(f"task {tid} depends on unmerged upstream task {dep} ({tasks[dep].get('status')})")

    for decision in open_decisions:
        if not isinstance(decision, dict):
            continue
        status = str(decision.get("status") or "open").lower()
        if status in ("resolved", "closed", "dismissed", "answered"):
            continue
        target = str(decision.get("task") or decision.get("node") or "")
        blocking = bool(decision.get("blocking")) or str(decision.get("urgency") or "").lower() == "blocker"
        if blocking or (target and (target in wave_set or target in wave_nodes)):
            did = decision.get("id") or "unknown"
            title = str(decision.get("title") or "")[:80]
            blockers.append(f"open decision {did} blocks wave {wave_number}: {title}")

    for conflict in detect_wave_owns_conflicts(plan):
        if conflict["wave"] == wave_number:
            warnings.append(f"overlapping owns in wave {wave_number}: {conflict['task_a']} and {conflict['task_b']}")

    if repo is not None and base_branch:
        from . import worktrees as wt
        if wt.head(Path(repo), base_branch) is None:
            blockers.append(f"base branch {base_branch!r} has no commit in {repo}")

    for tool in required_tools:
        if isinstance(tool, str) and tool.strip() and shutil.which(tool.strip()) is None:
            blockers.append(f"required tool {tool.strip()!r} is missing from PATH")

    return {"ok": not blockers, "wave": wave_number, "blockers": blockers, "warnings": warnings}


def _stage_wall_seconds(stage: rigor.Stage) -> int:
    # The three DeepSeek review stages run in parallel with review-cascade.
    if stage.name in rigor.REVIEW_DEEPSEEK_STAGES:
        return 0
    return STAGE_ETA_SECONDS.get(stage.role, 45)


def estimate_task_seconds(task: dict[str, Any], config: dict[str, Any] | None = None) -> tuple[int, int]:
    """Return (total_eta_s, remaining_eta_s) for one task from its rigor pipeline and owned paths."""
    cfg = {"rigor": "high", "profile": "balanced", **(config or {})}
    level, _ = rigor.effective(task, cfg)
    stages = rigor.pipeline(level)
    owns_bonus = min(120, len(task.get("owns") or ()) * OWNS_FILE_ETA_SECONDS)

    explicit = task.get("eta_s") or task.get("estimated_seconds")
    effort = str(task.get("effort") or "").upper()
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool) and explicit > 0:
        total = int(round(float(explicit)))
    elif effort in EFFORT_ETA_SECONDS:
        total = EFFORT_ETA_SECONDS[effort]
    else:
        total = sum(_stage_wall_seconds(s) for s in stages) + owns_bonus

    status = task.get("status")
    if status in (DONE, "skipped"):
        return total, 0

    recorded = task.get("stages") or {}
    if not recorded:
        return total, total

    elapsed_est = 0
    for stage in stages:
        wall = _stage_wall_seconds(stage)
        if stage.role == "implement":
            wall += owns_bonus
        st_rec = recorded.get(stage.name)
        if isinstance(st_rec, dict) and st_rec.get("status") == "passed":
            elapsed_est += wall
    remaining = max(5, total - elapsed_est) if status != DONE else 0
    return total, min(total, remaining)


def critical_path(plan: dict[str, Any], durations: dict[str, int] | None = None,
                  config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute the longest-path critical path, earliest/latest times, and per-task slack."""
    tasks = plan.get("tasks") or {}
    order = [tid for tid in (plan.get("order") or list(tasks)) if tid in tasks]
    if not order:
        return {
            "path": [],
            "total_seconds": 0,
            "earliest_start": {},
            "earliest_finish": {},
            "latest_start": {},
            "latest_finish": {},
            "slack": {},
        }

    weights: dict[str, int] = {}
    for tid in order:
        if durations is not None and tid in durations:
            weights[tid] = max(0, int(durations[tid]))
        else:
            weights[tid] = estimate_task_seconds(tasks[tid], config)[0]

    topo: list[str] = []
    seen: set[str] = set()
    for wave in plan.get("waves") or [order]:
        for tid in wave:
            if tid in tasks and tid not in seen:
                topo.append(tid)
                seen.add(tid)
    for tid in order:
        if tid not in seen:
            topo.append(tid)
            seen.add(tid)

    earliest_start: dict[str, int] = {}
    earliest_finish: dict[str, int] = {}
    predecessor: dict[str, str | None] = {}

    for tid in topo:
        deps = [d for d in (tasks[tid].get("deps") or ()) if d in earliest_finish]
        if not deps:
            earliest_start[tid] = 0
            predecessor[tid] = None
        else:
            best_dep = max(deps, key=lambda d: (earliest_finish[d], -topo.index(d)))
            earliest_start[tid] = earliest_finish[best_dep]
            predecessor[tid] = best_dep
        earliest_finish[tid] = earliest_start[tid] + weights[tid]

    end_tid = max(topo, key=lambda t: (earliest_finish[t], -topo.index(t)))
    total_seconds = earliest_finish[end_tid]

    path: list[str] = []
    cursor: str | None = end_tid
    while cursor is not None:
        path.append(cursor)
        cursor = predecessor.get(cursor)
    path.reverse()

    dependents: dict[str, list[str]] = {tid: [] for tid in topo}
    for tid in topo:
        for dep in tasks[tid].get("deps") or ():
            if dep in dependents:
                dependents[dep].append(tid)

    latest_finish: dict[str, int] = {}
    latest_start: dict[str, int] = {}
    for tid in reversed(topo):
        children = dependents[tid]
        if not children:
            latest_finish[tid] = total_seconds
        else:
            latest_finish[tid] = min(latest_start[c] for c in children)
        latest_start[tid] = latest_finish[tid] - weights[tid]

    slack = {tid: max(0, latest_start[tid] - earliest_start[tid]) for tid in topo}
    return {
        "path": path,
        "total_seconds": total_seconds,
        "earliest_start": earliest_start,
        "earliest_finish": earliest_finish,
        "latest_start": latest_start,
        "latest_finish": latest_finish,
        "slack": slack,
    }


def _wave_wall_seconds(task_ids: list[str], weights: dict[str, int], max_sessions: int) -> int:
    if not task_ids:
        return 0
    vals = sorted((max(0, weights.get(tid, 0)) for tid in task_ids), reverse=True)
    slots = max(1, int(max_sessions or 1))
    loads = [0] * min(slots, len(vals))
    for duration in vals:
        idx = min(range(len(loads)), key=loads.__getitem__)
        loads[idx] += duration
    return max(loads, default=0)


def describe_preview(plan: dict[str, Any], config: dict[str, Any] | None = None,
                     sessions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the deterministic DAG preview model: waves, parallel groups, critical path, and ETAs."""
    cfg = {"rigor": "high", "profile": "balanced", "max_sessions": 3, **(config or {})}
    tasks = plan.get("tasks") or {}
    order = [tid for tid in (plan.get("order") or list(tasks)) if tid in tasks]
    raw_waves = plan.get("waves") or ([order] if order else [])
    max_sessions = int(cfg.get("max_sessions") or 3)

    total_by_task: dict[str, int] = {}
    rem_by_task: dict[str, int] = {}
    for tid in order:
        tot, rem = estimate_task_seconds(tasks[tid], cfg)
        total_by_task[tid] = tot
        rem_by_task[tid] = rem

    cp_total = critical_path(plan, total_by_task, cfg)
    cp_remaining = critical_path(plan, rem_by_task, cfg)
    cp_set = set(cp_total["path"])

    wave_items: list[dict[str, Any]] = []
    cumulative_eta = 0
    cumulative_rem = 0
    waves_completed = 0

    for number, wave_ids in enumerate(raw_waves, start=1):
        valid_ids = [tid for tid in wave_ids if tid in tasks]
        merged_count = sum(1 for tid in valid_ids if tasks[tid].get("status") == DONE)
        active_count = sum(
            1 for tid in valid_ids
            if tasks[tid].get("status") in ("running", "verifying", "queued", "merging")
        )
        if valid_ids and merged_count == len(valid_ids):
            w_status = "merged"
            waves_completed += 1
        elif active_count > 0 or (merged_count > 0 and merged_count < len(valid_ids)):
            w_status = "running"
        elif any(tasks[tid].get("status") == "ready" for tid in valid_ids):
            w_status = "ready"
        else:
            w_status = "blocked"

        wave_eta_s = _wave_wall_seconds(valid_ids, total_by_task, max_sessions)
        wave_rem_s = _wave_wall_seconds(valid_ids, rem_by_task, max_sessions)
        cumulative_eta += wave_eta_s
        cumulative_rem += wave_rem_s

        wave_tasks: list[dict[str, Any]] = []
        for tid in valid_ids:
            t = tasks[tid]
            parallel_peers = [other for other in valid_ids if other != tid]
            conflicts = [
                other for other in parallel_peers
                if _owns_overlap(t.get("owns") or (), tasks[other].get("owns") or ())
            ]
            true_parallel = [other for other in parallel_peers if other not in conflicts]
            wave_tasks.append({
                "id": tid,
                "title": t.get("title") or tid,
                "goal": t.get("goal") or "",
                "status": t.get("status") or "blocked",
                "stage": t.get("stage"),
                "stages": t.get("stages") or {},
                "deps": list(t.get("deps") or ()),
                "owns": list(t.get("owns") or ()),
                "wave": number,
                "attempts": int(t.get("attempts") or 0),
                "dry_run": bool(t.get("dry_run")),
                "eta_s": total_by_task[tid],
                "remaining_eta_s": rem_by_task[tid],
                "earliest_start_s": cp_total["earliest_start"].get(tid, 0),
                "earliest_finish_s": cp_total["earliest_finish"].get(tid, 0),
                "slack_s": cp_total["slack"].get(tid, 0),
                "critical": tid in cp_set,
                "parallel": len(true_parallel) > 0,
                "parallel_with": true_parallel,
                "owns_conflict_with": conflicts,
            })

        wave_items.append({
            "wave": number,
            "status": w_status,
            "task_ids": valid_ids,
            "tasks": wave_tasks,
            "total_count": len(valid_ids),
            "merged_count": merged_count,
            "parallel": len(valid_ids) > 1,
            "parallel_count": len(valid_ids),
            "eta_s": wave_eta_s,
            "remaining_eta_s": wave_rem_s,
            "cumulative_eta_s": cumulative_eta,
            "cumulative_remaining_eta_s": cumulative_rem,
        })

    merged_total = sum(1 for tid in order if tasks[tid].get("status") == DONE)
    running_total = sum(
        1 for tid in order
        if tasks[tid].get("status") in ("running", "verifying", "queued", "merging")
    )
    return {
        "dry_run": bool(cfg.get("dry_run")),
        "rigor": cfg.get("rigor", "high"),
        "max_sessions": max_sessions,
        "total_tasks": len(order),
        "merged_tasks": merged_total,
        "running_tasks": running_total,
        "waves_total": len(wave_items),
        "waves_completed": waves_completed,
        "critical_path": cp_total["path"],
        "critical_path_eta_s": cp_total["total_seconds"],
        "remaining_eta_s": cp_remaining["total_seconds"],
        "waves": wave_items,
    }
