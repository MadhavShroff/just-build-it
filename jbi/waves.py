"""Plan validation and wave compilation.

A plan is an ordered list of task dicts. `compile_plan` validates the list, computes
Kahn topological levels in plan order, and returns the `plan` section of the state
document. A wave groups tasks for display only: the scheduler starts a task as soon
as its own dependencies are merged (`ready_tasks`).
"""

from __future__ import annotations

from typing import Iterable

from .protocol import TASK_ID, ProtocolError

REQUIRED = ("id", "title", "goal", "deps", "acceptance")
DONE = "merged"


def normalize(task: dict) -> dict:
    """Return a task with every field the state document expects."""
    return {
        "id": task["id"], "title": task["title"].strip(), "goal": task["goal"].strip(),
        "deps": list(task.get("deps", [])), "acceptance": task["acceptance"].strip(),
        "owns": list(task.get("owns", [])), "components": list(task.get("components", [])),
        "wave": 0, "status": "blocked", "stage": None, "attempts": 0,
        "override": {}, "branch": f"jbi/{task['id']}", "commit": None, "receipt": None,
    }


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


def compile_plan(tasks: list[dict], revision: int) -> dict:
    validate(tasks)
    normalized = {t["id"]: normalize(t) for t in tasks}
    waves = levels(tasks)
    for number, wave in enumerate(waves, start=1):
        for tid in wave:
            normalized[tid]["wave"] = number
    for task in normalized.values():
        task["status"] = "ready" if not task["deps"] else "blocked"
    return {"revision": revision, "tasks": normalized, "order": [t["id"] for t in tasks], "waves": waves}


def ready_tasks(plan: dict) -> list[str]:
    """Tasks whose dependencies are all merged and that have not started."""
    tasks = plan.get("tasks", {})
    return [tid for tid in plan.get("order", [])
            if tasks[tid]["status"] in ("blocked", "ready")
            and all(tasks[d]["status"] == DONE for d in tasks[tid]["deps"])]


def owns_conflict(plan: dict, tid: str, running: Iterable[str]) -> str | None:
    """Return a running task that owns a path that `tid` also owns, or None."""
    mine = set(plan["tasks"][tid]["owns"])
    for other in running:
        if other != tid and mine & set(plan["tasks"][other]["owns"]):
            return other
    return None
