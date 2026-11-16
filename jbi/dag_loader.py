"""Typed DAG loader for restricted-YAML wave files and `.planning/.../dag.json` files.

Reads either the restricted YAML format that `devloop-cli/wave-compiler` accepts or a
JSON task DAG (`.planning/.../dag.json`), validates task identifiers, dependencies,
cycles, owned paths, and explicit wave groups, and emits the plan structure that
`jbi.waves` compiles.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import waves
from .protocol import TASK_ID, ProtocolError

KIND: dict[str, str] = {k: "str" for k in ("project", "repo", "mode", "waves", "id", "title", "goal", "acceptance")}
KIND.update(deps="list", owns="list", components="list", wave_groups="any")
TASK_REQ = ("id", "title", "goal", "deps", "acceptance")
KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
ID_RE = TASK_ID


class Invalid(ProtocolError):
    """A malformed or contradictory DAG file."""

    def __init__(self, message: str, **details: Any):
        super().__init__("conflict", message, **details)


def _bad(line: int, msg: str) -> None:
    raise Invalid(f"line {line}: {msg}")


def _assign(target: dict[str, Any], key: str, rest: str, line: int) -> None:
    if key in target:
        _bad(line, f"duplicate field '{key}'")
    if not rest or rest[0] not in '"[':
        _bad(line, f"field '{key}' must be a JSON-quoted string or a JSON flow list")
    try:
        value, end = json.JSONDecoder().raw_decode(rest)
    except ValueError:
        _bad(line, "value is not valid restricted-YAML syntax")
    if rest[end:].strip():
        _bad(line, "value has trailing content on the line")
    if KIND[key] == "str" and not isinstance(value, str):
        _bad(line, f"field '{key}' must be one JSON-quoted string")
    if KIND[key] == "list" and (not isinstance(value, list) or not all(isinstance(e, str) for e in value)):
        _bad(line, f"field '{key}' must be a JSON flow list of strings")
    target[key] = value


def parse(text: str) -> dict[str, Any]:
    """Parse restricted-YAML text per PRD-FORMAT.md."""
    if "\t" in text:
        raise Invalid("file contains a tab; use spaces")
    doc: dict[str, Any] = {}
    tasks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    in_tasks = False
    for n, raw in enumerate(text.split("\n"), 1):
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        if not stripped:
            continue
        if stripped.startswith("#"):
            _bad(n, "comments are not allowed")
        if indent == 0:
            cur, in_tasks = None, False
            key, sep, rest = raw.partition(":")
            if key == "tasks":
                if rest.strip():
                    _bad(n, "tasks must be a block sequence of task items")
                if "tasks" in doc:
                    _bad(n, "duplicate field 'tasks'")
                doc["tasks"], in_tasks = "block", True
            elif key in KIND and sep and KEY_RE.match(key):
                _assign(doc, key, rest.strip(), n)
            else:
                _bad(n, f"unknown or malformed root field '{key}'")
        elif indent == 4 or (indent == 2 and stripped.startswith("- ")):
            if indent == 2:
                if not in_tasks:
                    _bad(n, "task item outside the tasks block")
                cur = {}
                tasks.append(cur)
                stripped = stripped[2:]
            elif cur is None:
                _bad(n, "indented line outside a task item")
            key, sep, rest = stripped.partition(":")
            if not sep or not KEY_RE.match(key) or key not in KIND or key == "wave_groups":
                _bad(n, f"unknown or malformed task field '{key}'")
            _assign(cur, key, rest.strip(), n)
        else:
            _bad(n, "unexpected indentation")
    doc["tasks"] = tasks
    return doc


def _validate_tasks_and_groups(tasks: list[dict[str, Any]], waves_mode: str,
                               wave_groups: Any = None, has_wave_groups: bool = False) -> None:
    if not tasks:
        raise Invalid("supply at least one task")
    ids = [t.get("id") if isinstance(t.get("id"), str) and t.get("id") else "?" for t in tasks]
    seen = {i for i in ids if i != "?" and ids.count(i) > 1}
    if seen:
        raise Invalid(f"duplicate task id '{sorted(seen)[0]}'")
    known = set(ids)
    for task in tasks:
        absent = [k for k in TASK_REQ if k not in task]
        if absent:
            raise Invalid(f"task '{task.get('id', '?')}' is missing field '{absent[0]}'")
        for name in ("title", "goal", "acceptance"):
            if not isinstance(task[name], str) or not task[name].strip():
                raise Invalid(f"task '{task['id']}' field '{name}' must be non-empty")
        if not isinstance(task["id"], str) or not ID_RE.match(task["id"]):
            raise Invalid(f"invalid task id '{task['id']}'")
        if not isinstance(task["deps"], list) or not all(isinstance(d, str) for d in task["deps"]):
            raise Invalid(f"task '{task['id']}' field 'deps' must be a list of strings")
        for dep in task["deps"]:
            if dep == task["id"]:
                raise Invalid(f"task '{dep}' depends on itself", cycle=[dep, dep])
            if dep not in known:
                raise Invalid(f"task '{task['id']}' depends on unknown task '{dep}'")
        if len(set(task["deps"])) != len(task["deps"]):
            raise Invalid(f"task '{task['id']}' repeats a dependency")
        owns = task.setdefault("owns", [])
        if not isinstance(owns, list) or not all(isinstance(p, str) for p in owns):
            raise Invalid(f"task '{task['id']}' field 'owns' must be a list of strings")
        for path in owns:
            if not path or path[0] == "/" or "\\" in path or ".." in path.split("/"):
                raise Invalid(f"task '{task['id']}' owns invalid path '{path}'")
        if len(set(owns)) != len(owns):
            raise Invalid(f"task '{task['id']}' repeats an owns path")
        task.setdefault("components", [])
    depmap = {t["id"]: t["deps"] for t in tasks}
    if waves_mode == "explicit":
        if not isinstance(wave_groups, list) or not wave_groups:
            raise Invalid("wave_groups must be a flow list of non-empty identifier lists")
        placed: dict[str, int] = {}
        for num, group in enumerate(wave_groups, 1):
            if not isinstance(group, list) or not group:
                raise Invalid(f"wave group {num} must be a non-empty list of identifiers")
            for tid in group:
                if not isinstance(tid, str) or tid not in known:
                    raise Invalid(f"wave group {num} holds unknown or invalid task '{tid}'")
                if tid in placed:
                    raise Invalid(f"task '{tid}' appears in groups {placed[tid]} and {num}")
                placed[tid] = num
        miss = [t for t in ids if t not in placed]
        if miss:
            raise Invalid(f"task '{miss[0]}' is missing from wave_groups")
        for tid, num in placed.items():
            late = [d for d in depmap[tid] if placed[d] >= num]
            if late:
                raise Invalid(f"task '{tid}' in group {num} needs '{late[0]}' in an earlier group")
    elif has_wave_groups:
        raise Invalid("waves 'auto' must not supply wave_groups")


def validate(doc: dict[str, Any]) -> dict[str, Any]:
    """Validate a parsed restricted-YAML document and verify it is acyclic."""
    for key in ("project", "repo", "mode", "waves"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            raise Invalid(f"root field '{key}' must be a non-empty string")
    if doc["mode"] not in ("new", "existing"):
        raise Invalid("mode must be 'new' or 'existing'")
    if doc["waves"] not in ("auto", "explicit"):
        raise Invalid("waves must be 'auto' or 'explicit'")
    tasks = doc.get("tasks")
    if not isinstance(tasks, list):
        raise Invalid("supply at least one task")
    _validate_tasks_and_groups(tasks, doc["waves"], doc.get("wave_groups"), "wave_groups" in doc)
    kahn(tasks)
    return doc


def kahn(tasks: list[dict[str, Any]]) -> tuple[dict[str, int], list[str]]:
    """Compute Kahn topological levels in task order, or raise Invalid naming a closed cycle."""
    order = [t["id"] for t in tasks]
    dependents: dict[str, list[str]] = {t: [] for t in order}
    waiting = {t["id"]: len(t["deps"]) for t in tasks}
    for task in tasks:
        for dep in task["deps"]:
            dependents[dep].append(task["id"])
    level = {t: 0 for t in order if not waiting[t]}
    queue, head = list(level), 0
    while head < len(queue):
        for tid in dependents[queue[head]]:
            level[tid] = max(level.get(tid, 0), level[queue[head]] + 1)
            waiting[tid] -= 1
            if not waiting[tid]:
                queue.append(tid)
        head += 1
    if len(level) == len(order):
        return level, order
    rest = set(order) - set(level)
    pos = {t: i for i, t in enumerate(order)}
    deps = {t["id"]: [d for d in t["deps"] if d in rest] for t in tasks if t["id"] in rest}
    path, shut = [min(rest, key=pos.get)], set()
    while path:
        hit = next((d for d in deps[path[-1]] if d in path), None)
        if hit:
            cycle = path[path.index(hit):] + [hit]
            raise Invalid("dependency cycle: " + " -> ".join(cycle), cycle=cycle)
        fresh = next((d for d in deps[path[-1]] if d not in shut), None)
        if fresh is None:
            shut.add(path.pop())
        else:
            path.append(fresh)
    raise Invalid("dependency cycle")


def _normalize_json_node(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise Invalid("each DAG node must be a JSON object")
    out = dict(node)
    if "goal" not in out:
        title = out.get("title") if isinstance(out.get("title"), str) else ""
        plan_path = out.get("path") if isinstance(out.get("path"), str) else ""
        spec_path = out.get("spec") if isinstance(out.get("spec"), str) else ""
        if title and plan_path and spec_path:
            out["goal"] = f"Implement {title} ({plan_path}; spec {spec_path})."
        elif title and plan_path:
            out["goal"] = f"Implement {title} ({plan_path})."
        elif title:
            out["goal"] = title
    if "acceptance" not in out:
        out["acceptance"] = "true"
    out.setdefault("owns", [])
    out.setdefault("components", [])
    return out


def parse_dag_json(text: str) -> dict[str, Any]:
    """Parse and validate a `.planning/.../dag.json` document."""
    try:
        raw = json.loads(text)
    except ValueError as error:
        raise Invalid(f"invalid DAG JSON: {error.args[0]}") from error
    if not isinstance(raw, dict):
        raise Invalid("DAG JSON root must be an object")
    raw_items = raw.get("nodes") if "nodes" in raw else raw.get("tasks")
    if isinstance(raw_items, dict):
        order = raw.get("order") if isinstance(raw.get("order"), list) else list(raw_items)
        items = [raw_items[tid] for tid in order if tid in raw_items]
    elif isinstance(raw_items, list):
        items = raw_items
    else:
        raise Invalid("DAG JSON must contain a 'nodes' or 'tasks' list")
    tasks = [_normalize_json_node(item) for item in items]
    explicit_waves = raw.get("wave_groups") if "wave_groups" in raw else raw.get("waves")
    waves_mode = "explicit" if isinstance(explicit_waves, list) else "auto"
    _validate_tasks_and_groups(
        tasks,
        waves_mode,
        explicit_waves if waves_mode == "explicit" else None,
        False,
    )
    kahn(tasks)
    doc: dict[str, Any] = {
        "project": str(raw.get("project") or raw.get("name") or "dag"),
        "repo": str(raw.get("repo") or raw.get("target") or "."),
        "mode": str(raw.get("mode") if raw.get("mode") in ("new", "existing") else "existing"),
        "waves": waves_mode,
        "tasks": tasks,
    }
    if waves_mode == "explicit":
        doc["wave_groups"] = [list(g) for g in explicit_waves]
    return doc


def _read_source(source: str | Path) -> tuple[str, bool]:
    """Return (text, is_json) from a Path, a file path string, or raw DAG text."""
    if isinstance(source, Path):
        path: Path | None = source.expanduser()
    elif isinstance(source, str) and "\n" not in source and not source.lstrip().startswith(("{", "[")):
        path = Path(source).expanduser()
    else:
        path = None
    if path is not None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise Invalid(f"cannot read {path}: {exc.strerror}") from exc
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Invalid("file is not valid UTF-8") from exc
        is_json = path.suffix.lower() == ".json" or text.lstrip().startswith(("{", "["))
        return text, is_json
    text = str(source)
    return text, text.lstrip().startswith(("{", "["))


def load_document(source: str | Path) -> dict[str, Any]:
    """Load and validate a restricted-YAML or `.planning/.../dag.json` DAG document."""
    text, is_json = _read_source(source)
    if is_json:
        return parse_dag_json(text)
    return validate(parse(text))


def load_tasks(source: str | Path) -> list[dict[str, Any]]:
    """Return the validated task list ready for `jbi.waves.compile_plan`."""
    return load_document(source)["tasks"]


def load(source: str | Path, *, revision: int = 1) -> dict[str, Any]:
    """Load a DAG from `source` and return the compiled `plan` dict for the state document."""
    doc = load_document(source)
    plan = waves.compile_plan(doc["tasks"], revision)
    if doc.get("waves") == "explicit" and doc.get("wave_groups"):
        groups = [list(group) for group in doc["wave_groups"]]
        plan["waves"] = groups
        for number, group in enumerate(groups, start=1):
            for tid in group:
                plan["tasks"][tid]["wave"] = number
    return plan


load_plan = load
