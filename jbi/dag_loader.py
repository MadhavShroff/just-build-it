"""Typed DAG loader backed by `/home/madhavshroff/src/devloop-cli/wave-compiler`.

Imports the canonical `wave-compiler` module as a library instead of duplicating its
restricted-YAML parser, schema validator, or Kahn cycle detector, and also normalizes
`.planning/.../dag.json` files through the same validator before emitting a `jbi.waves`
plan dictionary.
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
    try:
        validated = wave_compiler.validate(doc)
        wave_compiler.kahn(validated["tasks"])
    except wave_compiler.Invalid as exc:
        raise _wrap_invalid(exc) from exc
    for task in validated["tasks"]:
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
    """Parse a `.planning/.../dag.json` document and validate it with `wave_compiler`."""
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
    doc: dict[str, Any] = {
        "project": str(raw.get("project") or raw.get("name") or "dag"),
        "repo": str(raw.get("repo") or raw.get("target") or "."),
        "mode": str(raw.get("mode") if raw.get("mode") in ("new", "existing") else "existing"),
        "waves": waves_mode,
        "tasks": tasks,
    }
    if waves_mode == "explicit":
        doc["wave_groups"] = [list(g) for g in explicit_waves]
    return validate(doc)


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
