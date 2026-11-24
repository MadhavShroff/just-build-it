"""Typed DAG loader backed by `/home/madhavshroff/src/devloop-cli/wave-compiler`.

Imports the canonical `wave-compiler` module as a library instead of duplicating its
restricted-YAML parser, schema validator, or Kahn cycle detector, and also normalizes
`.planning/.../dag.json` files (including per-node `subtasks` / `batches` expansion)
through the same validator before emitting a `jbi.waves` plan dictionary.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any

from . import waves
from .protocol import ProtocolError

WAVE_COMPILER_PATH = Path(
    os.environ.get("JBI_WAVE_COMPILER", "/home/madhavshroff/src/devloop-cli/wave-compiler")
)


def _load_wave_compiler(path: Path = WAVE_COMPILER_PATH) -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("wave_compiler", str(path))
    spec = importlib.util.spec_from_loader("wave_compiler", loader)
    if spec is None:
        raise RuntimeError(f"Cannot load wave-compiler from {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


wave_compiler = _load_wave_compiler()


class Invalid(ProtocolError, wave_compiler.Invalid):
    """A malformed or contradictory DAG file."""

    def __init__(self, message: str, **details: Any):
        ProtocolError.__init__(self, "conflict", message, **details)
        wave_compiler.Invalid.__init__(self, message)


def _wrap_invalid(exc: Exception) -> Invalid:
    msg = str(exc)
    if msg.startswith("dependency cycle: "):
        cycle = [part.strip() for part in msg.removeprefix("dependency cycle: ").split("->")]
        return Invalid(msg, cycle=cycle)
    return Invalid(msg)


def parse(text: str) -> dict[str, Any]:
    """Parse restricted-YAML text by calling `wave_compiler.parse`."""
    try:
        return wave_compiler.parse(text)
    except wave_compiler.Invalid as exc:
        raise _wrap_invalid(exc) from exc


def validate(doc: dict[str, Any]) -> dict[str, Any]:
    """Validate a parsed DAG document and verify it is acyclic via `wave_compiler`."""
    raw_tasks = doc.get("tasks") if isinstance(doc, dict) else None
    if isinstance(raw_tasks, list):
        for raw_task in raw_tasks:
            if isinstance(raw_task, dict) and "acceptance" in raw_task:
                acc = raw_task["acceptance"]
                if not isinstance(acc, str) or not acc.strip():
                    tid = raw_task.get("id") or "?"
                    raise Invalid(f"task '{tid}' field 'acceptance' must be a non-empty string")
    try:
        validated = wave_compiler.validate(doc)
        wave_compiler.kahn(validated["tasks"])
    except wave_compiler.Invalid as exc:
        raise _wrap_invalid(exc) from exc
    for task in validated["tasks"]:
        acc = task.get("acceptance")
        if not isinstance(acc, str) or not acc.strip():
            tid = task.get("id") or "?"
            raise Invalid(f"task '{tid}' field 'acceptance' must be a non-empty string")
        task.setdefault("components", [])
    return validated


def kahn(tasks: list[dict[str, Any]]) -> tuple[dict[str, int], list[str]]:
    """Compute Kahn topological levels in task order via `wave_compiler.kahn`."""
    try:
        return wave_compiler.kahn(tasks)
    except wave_compiler.Invalid as exc:
        raise _wrap_invalid(exc) from exc


def main(argv: list[str] | None = None) -> int:
    """Run the `wave-compiler` CLI entry point directly."""
    return int(wave_compiler.main(argv))


def _normalize_json_node(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise Invalid("each DAG node must be a JSON object")
    out = dict(node)
    if "goal" not in out or not isinstance(out.get("goal"), str) or not out["goal"].strip():
        title = out.get("title") if isinstance(out.get("title"), str) else ""
        plan_path = out.get("path") if isinstance(out.get("path"), str) else ""
        spec_path = out.get("spec") if isinstance(out.get("spec"), str) else ""
        if title and plan_path and spec_path:
            out["goal"] = f"Implement {title} ({plan_path}; spec {spec_path})."
        elif title and plan_path:
            out["goal"] = f"Implement {title} ({plan_path})."
        elif title:
            out["goal"] = title
    if "acceptance" in out:
        raw_acc = out["acceptance"]
        if not isinstance(raw_acc, str) or not raw_acc.strip():
            tid = out.get("id") or "?"
            raise Invalid(f"task '{tid}' field 'acceptance' must be a non-empty string")
    else:
        out["acceptance"] = "true"
    out.setdefault("deps", [])
    out.setdefault("owns", [])
    out.setdefault("components", [])
    _validate_override(out)
    return out


# A node may pin the model and provider its stages run on (founder order
# 2026-11-17: "a way to attach a particular dag node to a particular model and
# provider"). It is a routing choice, never a speed choice: the scheduler still
# defaults to the account preference, and an override names one provider for one node.
_OVERRIDE_KEYS = ("prefer", "pin", "profile")
_PROVIDERS = ("agy", "claude", "codex", "openai-compatible", "simulator")


def _validate_override(node: dict[str, Any]) -> None:
    """Refuse a malformed override, so a typo cannot silently fall back to the account preference.

    A silent fallback is the dangerous case: a node meant for agy would run on
    whatever the account preference names, and nothing would say so.
    """
    override = node.get("override")
    if override is None:
        return
    tid = node.get("id") or "?"
    if not isinstance(override, dict):
        raise Invalid(f"task {tid}: override must be an object, not {type(override).__name__}")
    unknown = sorted(set(override) - set(_OVERRIDE_KEYS))
    if unknown:
        raise Invalid(f"task {tid}: override names unknown key(s) {', '.join(unknown)}; "
                      f"allowed: {', '.join(_OVERRIDE_KEYS)}")
    prefer, pin = override.get("prefer"), override.get("pin")
    if prefer is not None and pin is not None:
        raise Invalid(f"task {tid}: override holds both prefer and pin; a task takes one "
                      f"or the other, because a pin is a hard rule and a preference is not")
    if prefer is not None:
        if not isinstance(prefer, list) or not prefer:
            raise Invalid(f"task {tid}: override.prefer must be a non-empty list")
        for item in prefer:
            if not isinstance(item, dict) or not item.get("provider"):
                raise Invalid(f"task {tid}: each override.prefer entry needs a provider")
            _check_provider(tid, item.get("provider"), "prefer")
    if pin is not None:
        if not isinstance(pin, dict):
            raise Invalid(f"task {tid}: override.pin must be an object")
        _check_provider(tid, pin.get("provider"), "pin")
        if not isinstance(pin.get("model"), str) or not pin["model"].strip():
            raise Invalid(f"task {tid}: override.pin needs a model, because a pin waits for "
                          f"one exact model rather than a tier")
    if override.get("profile") is not None and not isinstance(override["profile"], str):
        raise Invalid(f"task {tid}: override.profile must be a string")


def _check_provider(tid: str, provider: Any, where: str) -> None:
    if not isinstance(provider, str) or provider not in _PROVIDERS:
        raise Invalid(f"task {tid}: override.{where} provider {provider!r} is not one of "
                      f"{', '.join(_PROVIDERS)}")


def expand_node_subtasks(nodes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Expand any node with `subtasks` or `batches` into child tasks that inherit parent `deps`."""
    raw_sub_counts: dict[str, int] = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise Invalid("each DAG node must be a JSON object")
        subs = node.get("subtasks") if "subtasks" in node else node.get("batches")
        if subs is None:
            continue
        if not isinstance(subs, list) or not subs:
            raise Invalid(f"node '{node.get('id', '?')}' subtasks/batches must be a non-empty list")
        for sub in subs:
            if not isinstance(sub, dict):
                raise Invalid(f"node '{node.get('id', '?')}' subtask must be an object")
            sid = str(sub.get("id") or "").strip()
            if sid:
                raw_sub_counts[sid] = raw_sub_counts.get(sid, 0) + 1

    expanded_by_parent: dict[str, list[str]] = {}
    prepared: list[tuple[dict[str, Any], list[dict[str, Any]] | None]] = []

    for node in nodes:
        norm_parent = _normalize_json_node(node)
        pid = str(norm_parent.get("id") or "")
        subs = node.get("subtasks") if "subtasks" in node else node.get("batches")
        if not subs:
            prepared.append((norm_parent, None))
            continue

        alias_map: dict[str, str] = {}
        child_specs: list[tuple[dict[str, Any], str, str]] = []
        for sub in subs:
            raw_id = str(sub.get("id") or "").strip()
            if not raw_id:
                raise Invalid(f"node '{pid}' has a subtask missing 'id'")
            batch_code = str(sub.get("batch") or raw_id).strip()
            if raw_sub_counts.get(raw_id, 0) > 1 and not raw_id.startswith((f"{pid}-", f"{pid}_")):
                full_id = f"{pid}-{raw_id}"
            else:
                full_id = raw_id
            alias_map[raw_id] = full_id
            alias_map[batch_code] = full_id
            alias_map[f"{pid}-{batch_code}"] = full_id
            child_specs.append((sub, full_id, batch_code))

        parent_deps = [str(d) for d in (norm_parent.get("deps") or ())]
        children: list[dict[str, Any]] = []
        child_ids: list[str] = []

        for sub, full_id, batch_code in child_specs:
            raw_deps = sub.get("deps")
            if raw_deps is None:
                raise Invalid(f"subtask '{full_id}' is missing field 'deps'")
            if not isinstance(raw_deps, list) or not all(isinstance(d, str) for d in raw_deps):
                raise Invalid(f"subtask '{full_id}' field 'deps' must be a list of strings")
            resolved_sub_deps = [alias_map.get(d, d) for d in raw_deps]
            combined_deps: list[str] = list(parent_deps)
            for dep in resolved_sub_deps:
                if dep not in combined_deps:
                    combined_deps.append(dep)

            if "acceptance" in sub:
                sub_acc = sub["acceptance"]
                if not isinstance(sub_acc, str) or not sub_acc.strip():
                    raise Invalid(f"subtask '{full_id}' field 'acceptance' must be a non-empty string")
                child_acceptance = sub_acc
            else:
                child_acceptance = norm_parent.get("acceptance") or "true"

            child_raw = {
                **{k: v for k, v in norm_parent.items() if k not in ("subtasks", "batches", "wave")},
                **sub,
                "id": full_id,
                "node": sub.get("node") or pid,
                "batch": batch_code,
                "deps": combined_deps,
                "owns": list(sub.get("owns") if "owns" in sub else (norm_parent.get("owns") or ())),
                "acceptance": child_acceptance,
            }
            if "goal" not in sub or not str(sub.get("goal") or "").strip():
                child_raw["goal"] = str(sub.get("title") or norm_parent.get("goal") or full_id)
            children.append(_normalize_json_node(child_raw))
            child_ids.append(full_id)

        expanded_by_parent[pid] = child_ids
        prepared.append((norm_parent, children))

    if not expanded_by_parent:
        return [parent for parent, _ in prepared], False

    out_tasks: list[dict[str, Any]] = []
    for parent, children in prepared:
        items_to_emit = children if children is not None else [parent]
        for item in items_to_emit:
            rewritten_deps: list[str] = []
            for dep in item.get("deps") or ():
                if dep in expanded_by_parent:
                    for expanded_id in expanded_by_parent[dep]:
                        if expanded_id not in rewritten_deps:
                            rewritten_deps.append(expanded_id)
                elif dep not in rewritten_deps:
                    rewritten_deps.append(dep)
            item["deps"] = rewritten_deps
            out_tasks.append(item)

    return out_tasks, True


def parse_dag_json(text: str) -> dict[str, Any]:
    """Parse a `.planning/.../dag.json` document, expand subtasks/batches, and validate it."""
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
    tasks, expanded = expand_node_subtasks(items)
    raw_completed = raw.get("completed_deps") if "completed_deps" in raw else raw.get("satisfied_deps")
    if raw_completed is not None:
        if not isinstance(raw_completed, list) or not all(isinstance(d, str) for d in raw_completed):
            raise Invalid("field 'completed_deps' must be a list of strings")
        completed_set = set(raw_completed)
        active_ids = {str(t.get("id") or "") for t in tasks if isinstance(t, dict)}
        for task in tasks:
            deps = task.get("deps")
            if isinstance(deps, list):
                upstream = [d for d in deps if d in completed_set and d not in active_ids]
                if upstream:
                    existing_upstream = list(task.get("upstream_deps") or ())
                    for dep in upstream:
                        if dep not in existing_upstream:
                            existing_upstream.append(dep)
                    task["upstream_deps"] = existing_upstream
                    task["deps"] = [d for d in deps if d not in completed_set or d in active_ids]
    explicit_waves = raw.get("wave_groups") if "wave_groups" in raw else raw.get("waves")
    # A subtask-expanded doc never keeps manual waves: the groups name parent ids
    # the expansion replaced, so the plan recomputes its waves (auto).
    named_mode = str(raw.get("waves") or "").strip().lower()
    if named_mode in ("auto", "explicit") and not expanded:
        waves_mode = named_mode          # the author named the mode; keep it
    elif isinstance(explicit_waves, list) and not expanded:
        waves_mode = "explicit"
    else:
        waves_mode = "auto"
    # Manual wave_groups ride along in explicit and in auto mode alike: the
    # compiler honors the author's grouping whenever it is usable (M1). The
    # wave-compiler schema requires wave_groups under 'explicit' and refuses
    # them under 'auto': explicit groups validate inside the document, auto
    # groups attach only after its validation ran.
    manual_groups: list[list[str]] | None = None
    if isinstance(explicit_waves, list) and not expanded and ("wave_groups" in raw or waves_mode == "explicit"):
        manual_groups = [list(g) for g in explicit_waves]
    doc: dict[str, Any] = {
        "project": str(raw.get("project") or raw.get("name") or "dag"),
        "repo": str(raw.get("repo") or raw.get("target") or "."),
        "mode": str(raw.get("mode") if raw.get("mode") in ("new", "existing") else "existing"),
        "waves": waves_mode,
        "tasks": tasks,
    }
    # The DAG names the branch every task cuts from ("base") and the branch where
    # verified work lands ("branch"). The --base/--branch flags outrank these.
    if str(raw.get("base") or "").strip():
        doc["base"] = str(raw["base"]).strip()
    if str(raw.get("branch") or "").strip():
        doc["branch"] = str(raw["branch"]).strip()
    if manual_groups is not None and waves_mode == "explicit":
        doc["wave_groups"] = manual_groups
    validated = validate(doc)
    if manual_groups is not None and "wave_groups" not in validated:
        validated["wave_groups"] = manual_groups
    return validated


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


def validate_strict_spec(tasks: list[dict[str, Any]]) -> None:
    """Enforce self-contained execution spec fields (self-contained spec check)."""
    context_keys = ("contracts", "invariants", "test_cases", "done_criteria", "spec_details")
    for task in tasks:
        tid = str(task.get("id") or "?")
        goal = str(task.get("goal") or "").strip()
        if len(goal) < 20:
            raise Invalid(f"task '{tid}' strict spec requires a detailed 'goal' (>= 20 chars)", task=tid)
        owns = [p for p in (task.get("owns") or ()) if isinstance(p, str) and p.strip()]
        if not owns:
            raise Invalid(f"task '{tid}' strict spec requires at least one declared 'owns' path", task=tid)
        acc = str(task.get("acceptance") or "").strip()
        if not acc or acc == "true":
            raise Invalid(f"task '{tid}' strict spec requires a real executable 'acceptance' command", task=tid)
        if not any(task.get(k) for k in context_keys):
            raise Invalid(
                f"task '{tid}' strict spec requires at least one of {', '.join(context_keys)}",
                task=tid,
            )


def load(source: str | Path, *, revision: int = 1, strict_spec: bool = False,
         forbid_wave_owns_conflicts: bool = False) -> dict[str, Any]:
    """Load a DAG from `source` and return the compiled `plan` dict for the state document."""
    doc = load_document(source)
    if strict_spec:
        validate_strict_spec(doc["tasks"])
    plan = waves.compile_plan(doc["tasks"], revision)
    # Manual groups apply in explicit and in auto mode alike (M1); unnamed tasks
    # keep the dependency-order waves compile_plan computed for them.
    if doc.get("wave_groups"):
        groups = [list(group) for group in doc["wave_groups"]]
        plan["waves"] = groups
        for number, group in enumerate(groups, start=1):
            for tid in group:
                if tid in plan["tasks"]:
                    plan["tasks"][tid]["wave"] = number
    if forbid_wave_owns_conflicts:
        conflicts = waves.detect_wave_owns_conflicts(plan)
        if conflicts:
            first = conflicts[0]
            raise Invalid(
                f"wave {first['wave']} has overlapping owns between {first['task_a']} and {first['task_b']}",
                conflicts=conflicts,
            )
    return plan


load_plan = load
