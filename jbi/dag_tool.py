"""CLI utility for constructing, inspecting, and editing `just-build-it` DAG files.

Subcommands:
  - `new`           Create a new DAG file (`.json` or `.yaml`).
  - `add-task`      Add a task or node and validate the resulting DAG.
  - `add-dep`       Add a dependency edge between two tasks and validate.
  - `remove-task`   Remove a task (and clean explicit wave groups) and validate.
  - `set-wave`      Place tasks to a 1-based explicit wave group and validate.
  - `add-subtask`   Add a subtask/batch under a parent node and validate expansion.
  - `list-subtasks` List subtasks/batches defined on one or all nodes.
  - `validate`      Validate a DAG file and report wave count and task count.
  - `show`          Render the compiled waves, parallel tasks, critical path, and ETAs.

Every mutating command validates the edited document through `jbi.dag_loader.load` before
writing to disk and refuses to write an invalid DAG.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import dag_loader, waves


def _split_csv(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for raw in values:
        for part in str(raw).split(","):
            item = part.strip()
            if item:
                out.append(item)
    return out


def _is_yaml_path(path: Path, raw_text: str | None = None) -> bool:
    if path.suffix.lower() in (".yaml", ".yml"):
        return True
    if path.suffix.lower() == ".json":
        return False
    if raw_text is not None:
        return not raw_text.lstrip().startswith("{")
    return False


def _dump_restricted_yaml(doc: dict[str, Any]) -> str:
    lines: list[str] = [
        f'project: {json.dumps(str(doc.get("project") or "dag"))}',
        f'repo: {json.dumps(str(doc.get("repo") or "."))}',
        f'mode: {json.dumps(str(doc.get("mode") or "existing"))}',
        f'waves: {json.dumps(str(doc.get("waves") or "auto"))}',
    ]
    if doc.get("waves") == "explicit" and isinstance(doc.get("wave_groups"), list):
        lines.append("wave_groups:")
        for group in doc["wave_groups"]:
            lines.append(f"  - {json.dumps([str(x) for x in group])}")
    tasks = doc.get("tasks") if "tasks" in doc else doc.get("nodes") or []
    lines.append("tasks:")
    for task in tasks:
        lines.append(f'  - id: {json.dumps(str(task["id"]))}')
        lines.append(f'    title: {json.dumps(str(task["title"]))}')
        goal = str(task.get("goal") or task["title"])
        lines.append(f'    goal: {json.dumps(goal)}')
        deps = [str(d) for d in (task.get("deps") or ())]
        lines.append(f"    deps: {json.dumps(deps)}")
        acceptance = str(task.get("acceptance") or "true")
        lines.append(f'    acceptance: {json.dumps(acceptance)}')
        owns = [str(p) for p in (task.get("owns") or ())]
        lines.append(f"    owns: {json.dumps(owns)}")
    return "\n".join(lines) + "\n"


def _read_raw_doc(path: Path) -> tuple[dict[str, Any], bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise dag_loader.Invalid(f"cannot read {path}: {exc.strerror}") from exc
    is_yaml = _is_yaml_path(path, text)
    if is_yaml:
        doc = dag_loader.parse(text)
        return doc, True
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise dag_loader.Invalid(f"invalid DAG JSON: {exc.args[0]}") from exc
    if not isinstance(raw, dict):
        raise dag_loader.Invalid("DAG JSON root must be an object")
    return raw, False


def _items_list(doc: dict[str, Any]) -> list[dict[str, Any]]:
    key = "nodes" if "nodes" in doc else "tasks"
    if key not in doc or not isinstance(doc[key], list):
        doc[key] = []
    return doc[key]


def _serialize_doc(doc: dict[str, Any], is_yaml: bool) -> str:
    if is_yaml:
        return _dump_restricted_yaml(doc)
    return json.dumps(doc, indent=2) + "\n"


def _validate_and_write(path: Path, doc: dict[str, Any], is_yaml: bool, *, allow_empty: bool = False) -> dict[str, Any] | None:
    text = _serialize_doc(doc, is_yaml)
    items = doc.get("nodes") if "nodes" in doc else doc.get("tasks")
    if allow_empty and isinstance(items, list) and len(items) == 0:
        for field in ("project", "repo", "mode"):
            val = doc.get(field)
            if not isinstance(val, str) or not val.strip():
                raise dag_loader.Invalid(f"missing or invalid '{field}'")
        if doc.get("mode") not in ("new", "existing"):
            raise dag_loader.Invalid("field 'mode' must be 'new' or 'existing'")
        if doc.get("waves") not in (None, "auto", "explicit") and not isinstance(doc.get("waves"), list):
            raise dag_loader.Invalid("field 'waves' must be 'auto' or 'explicit'")
        plan = None
    else:
        plan = dag_loader.load(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return plan


def cmd_new(args: argparse.Namespace) -> int:
    path = Path(args.file)
    is_yaml = _is_yaml_path(path)
    doc: dict[str, Any] = {
        "project": args.project,
        "repo": args.repo,
        "mode": args.mode,
        "waves": args.waves,
        "tasks": [],
    }
    if args.waves == "explicit":
        doc["wave_groups"] = []
    if args.id:
        title = args.title or args.id
        task: dict[str, Any] = {
            "id": args.id,
            "title": title,
            "goal": args.goal or title,
            "deps": _split_csv(args.deps),
            "acceptance": args.acceptance or "true",
            "owns": _split_csv(args.owns),
        }
        doc["tasks"].append(task)
        if args.waves == "explicit":
            doc["wave_groups"] = [[args.id]]
    _validate_and_write(path, doc, is_yaml, allow_empty=not bool(args.id))
    return 0


def cmd_add_task(args: argparse.Namespace) -> int:
    path = Path(args.file)
    doc, is_yaml = _read_raw_doc(path)
    items = _items_list(doc)
    task: dict[str, Any] = {
        "id": args.id,
        "title": args.title,
        "goal": args.goal or args.title,
        "deps": _split_csv(args.deps),
        "acceptance": args.acceptance or "true",
        "owns": _split_csv(args.owns),
    }
    if args.node:
        task["node"] = args.node
    if args.batch:
        task["batch"] = args.batch
    if args.effort:
        task["effort"] = args.effort
    if args.owner:
        task["owner"] = args.owner
    items.append(task)
    if doc.get("waves") == "explicit" and isinstance(doc.get("wave_groups"), list):
        if args.wave is not None:
            idx = max(1, int(args.wave)) - 1
            while len(doc["wave_groups"]) <= idx:
                doc["wave_groups"].append([])
            doc["wave_groups"][idx].append(args.id)
        else:
            doc["wave_groups"].append([args.id])
    _validate_and_write(path, doc, is_yaml)
    return 0


def cmd_add_dep(args: argparse.Namespace) -> int:
    path = Path(args.file)
    doc, is_yaml = _read_raw_doc(path)
    items = _items_list(doc)
    target_id = args.task
    dep_id = args.dep
    found = None
    for item in items:
        if item.get("id") == target_id:
            found = item
            break
    if found is None:
        raise dag_loader.Invalid(f"unknown task '{target_id}'")
    deps = list(found.get("deps") or ())
    if dep_id in deps:
        raise dag_loader.Invalid(f"task '{target_id}' repeats a dependency on '{dep_id}'")
    deps.append(dep_id)
    found["deps"] = deps
    _validate_and_write(path, doc, is_yaml)
    return 0


def cmd_remove_task(args: argparse.Namespace) -> int:
    path = Path(args.file)
    doc, is_yaml = _read_raw_doc(path)
    items = _items_list(doc)
    target_id = args.id
    kept = [item for item in items if item.get("id") != target_id]
    if len(kept) == len(items):
        raise dag_loader.Invalid(f"unknown task '{target_id}'")
    if args.prune_deps:
        for item in kept:
            if isinstance(item.get("deps"), list):
                item["deps"] = [d for d in item["deps"] if d != target_id]
    key = "nodes" if "nodes" in doc else "tasks"
    doc[key] = kept
    for wkey in ("wave_groups", "waves"):
        if isinstance(doc.get(wkey), list):
            cleaned = [
                [tid for tid in group if tid != target_id]
                for group in doc[wkey]
                if isinstance(group, list)
            ]
            doc[wkey] = [group for group in cleaned if group]
    _validate_and_write(path, doc, is_yaml)
    return 0


def cmd_set_wave(args: argparse.Namespace) -> int:
    path = Path(args.file)
    doc, is_yaml = _read_raw_doc(path)
    items = _items_list(doc)
    wave_num = int(args.wave)
    if wave_num < 1:
        raise dag_loader.Invalid("wave number must be >= 1")
    target_tasks = _split_csv(args.tasks)
    if not target_tasks:
        raise dag_loader.Invalid("set-wave requires at least one task id in --tasks")

    existing_groups = doc.get("wave_groups")
    if not isinstance(existing_groups, list) or not existing_groups:
        compiled = dag_loader.load(_serialize_doc(doc, is_yaml))
        existing_groups = [list(g) for g in compiled["waves"]]
    else:
        existing_groups = [list(g) for g in existing_groups]

    target_set = set(target_tasks)
    stripped: list[list[str]] = [
        [tid for tid in group if tid not in target_set]
        for group in existing_groups
    ]
    while len(stripped) < wave_num:
        stripped.append([])
    for tid in target_tasks:
        if tid not in stripped[wave_num - 1]:
            stripped[wave_num - 1].append(tid)
    final_groups = [group for group in stripped if group]
    doc["waves"] = "explicit"
    doc["wave_groups"] = final_groups
    for idx, group in enumerate(final_groups, start=1):
        for item in items:
            if item.get("id") in group and "wave" in item:
                item["wave"] = idx
    _validate_and_write(path, doc, is_yaml)
    return 0


def cmd_add_subtask(args: argparse.Namespace) -> int:
    path = Path(args.file)
    doc, is_yaml = _read_raw_doc(path)
    if is_yaml:
        raise dag_loader.Invalid("subtasks/batches are supported on JSON DAG files")
    items = _items_list(doc)
    parent = None
    for item in items:
        if item.get("id") == args.node:
            parent = item
            break
    if parent is None:
        raise dag_loader.Invalid(f"unknown parent node '{args.node}'")
    key = "batches" if "batches" in parent and "subtasks" not in parent else "subtasks"
    subs = parent.get(key)
    if not isinstance(subs, list):
        subs = []
        parent[key] = subs
    sub: dict[str, Any] = {
        "id": args.id,
        "title": args.title,
        "deps": _split_csv(args.deps),
        "owns": _split_csv(args.owns),
    }
    if args.goal:
        sub["goal"] = args.goal
    if args.acceptance:
        sub["acceptance"] = args.acceptance
    if args.batch:
        sub["batch"] = args.batch
    if args.effort:
        sub["effort"] = args.effort
    subs.append(sub)
    _validate_and_write(path, doc, is_yaml=False)
    return 0


def cmd_list_subtasks(args: argparse.Namespace) -> int:
    path = Path(args.file)
    doc, _ = _read_raw_doc(path)
    items = _items_list(doc)
    rows: list[dict[str, Any]] = []
    for item in items:
        nid = str(item.get("id") or "")
        if args.node and nid != args.node:
            continue
        subs = item.get("subtasks") if "subtasks" in item else item.get("batches")
        if not isinstance(subs, list):
            continue
        for sub in subs:
            if isinstance(sub, dict):
                rows.append({
                    "node": nid,
                    "id": sub.get("id"),
                    "batch": sub.get("batch") or sub.get("id"),
                    "title": sub.get("title"),
                    "deps": list(sub.get("deps") or ()),
                    "owns": list(sub.get("owns") or ()),
                    "acceptance": sub.get("acceptance") or item.get("acceptance") or "true",
                })
    if args.node and not any(str(i.get("id") or "") == args.node for i in items):
        raise dag_loader.Invalid(f"unknown parent node '{args.node}'")
    if args.json:
        sys.stdout.write(json.dumps(rows, indent=2) + "\n")
    else:
        for row in rows:
            deps_txt = ",".join(row["deps"]) if row["deps"] else "-"
            sys.stdout.write(f'{row["node"]}\t{row["id"]}\tdeps={deps_txt}\t{row["title"]}\n')
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    plan = dag_loader.load(Path(args.file))
    preview = waves.describe_preview(plan)
    summary = {
        "ok": True,
        "total_tasks": preview["total_tasks"],
        "waves_total": preview["waves_total"],
        "waves": [
            {"wave": w["wave"], "count": w["total_count"], "tasks": w["task_ids"]}
            for w in preview["waves"]
        ],
        "critical_path": preview["critical_path"],
        "critical_path_eta_s": preview["critical_path_eta_s"],
    }
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    else:
        counts = ", ".join(f'W{w["wave"]}={w["count"]}' for w in summary["waves"])
        sys.stdout.write(
            f'OK: {summary["total_tasks"]} tasks in {summary["waves_total"]} waves ({counts})\n'
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    plan = dag_loader.load(Path(args.file))
    preview = waves.describe_preview(plan)
    if args.json:
        sys.stdout.write(json.dumps(preview, indent=2) + "\n")
        return 0
    sys.stdout.write(
        f'Tasks: {preview["total_tasks"]} | Waves: {preview["waves_total"]} | '
        f'Critical path: {" -> ".join(preview["critical_path"])} ({preview["critical_path_eta_s"]}s)\n'
    )
    for w in preview["waves"]:
        mode = f'parallel x{w["total_count"]}' if w["parallel"] else "serial"
        sys.stdout.write(f'Wave {w["wave"]} ({mode}, eta {w["eta_s"]}s): {", ".join(w["task_ids"])}\n')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jbi.dag_tool", description="Construct, validate, and edit JBI DAG files.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="Create a new DAG file")
    p_new.add_argument("file")
    p_new.add_argument("--project", default="dag")
    p_new.add_argument("--repo", default=".")
    p_new.add_argument("--mode", choices=("new", "existing"), default="existing")
    p_new.add_argument("--waves", choices=("auto", "explicit"), default="auto")
    p_new.add_argument("--id", default=None)
    p_new.add_argument("--title", default=None)
    p_new.add_argument("--goal", default=None)
    p_new.add_argument("--deps", action="append", default=[])
    p_new.add_argument("--owns", action="append", default=[])
    p_new.add_argument("--acceptance", default="true")
    p_new.set_defaults(func=cmd_new)

    p_add = sub.add_parser("add-task", help="Add a task to a DAG file")
    p_add.add_argument("file")
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--goal", default=None)
    p_add.add_argument("--deps", action="append", default=[])
    p_add.add_argument("--owns", action="append", default=[])
    p_add.add_argument("--acceptance", default="true")
    p_add.add_argument("--node", default=None)
    p_add.add_argument("--batch", default=None)
    p_add.add_argument("--effort", default=None)
    p_add.add_argument("--owner", default=None)
    p_add.add_argument("--wave", type=int, default=None)
    p_add.set_defaults(func=cmd_add_task)

    p_dep = sub.add_parser("add-dep", help="Add a dependency edge to a task")
    p_dep.add_argument("file")
    p_dep.add_argument("--task", required=True)
    p_dep.add_argument("--dep", required=True)
    p_dep.set_defaults(func=cmd_add_dep)

    p_rem = sub.add_parser("remove-task", help="Remove a task from a DAG file")
    p_rem.add_argument("file")
    p_rem.add_argument("--id", required=True)
    p_rem.add_argument("--prune-deps", action="store_true")
    p_rem.set_defaults(func=cmd_remove_task)

    p_wave = sub.add_parser("set-wave", help="Place tasks to an explicit 1-based wave")
    p_wave.add_argument("file")
    p_wave.add_argument("--wave", type=int, required=True)
    p_wave.add_argument("--tasks", action="append", required=True)
    p_wave.set_defaults(func=cmd_set_wave)

    p_sub = sub.add_parser("add-subtask", help="Add a subtask/batch to a node in a JSON DAG")
    p_sub.add_argument("file")
    p_sub.add_argument("--node", required=True)
    p_sub.add_argument("--id", required=True)
    p_sub.add_argument("--title", required=True)
    p_sub.add_argument("--goal", default=None)
    p_sub.add_argument("--deps", action="append", default=[])
    p_sub.add_argument("--owns", action="append", default=[])
    p_sub.add_argument("--acceptance", default=None)
    p_sub.add_argument("--batch", default=None)
    p_sub.add_argument("--effort", default=None)
    p_sub.set_defaults(func=cmd_add_subtask)

    p_lsub = sub.add_parser("list-subtasks", help="List subtasks/batches on a JSON DAG")
    p_lsub.add_argument("file")
    p_lsub.add_argument("--node", default=None)
    p_lsub.add_argument("--json", action="store_true")
    p_lsub.set_defaults(func=cmd_list_subtasks)

    p_val = sub.add_parser("validate", help="Validate a DAG file")
    p_val.add_argument("file")
    p_val.add_argument("--json", action="store_true")
    p_val.set_defaults(func=cmd_validate)

    p_show = sub.add_parser("show", help="Display compiled waves and critical path")
    p_show.add_argument("file")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except dag_loader.Invalid as exc:
        sys.stderr.write(f"dag_tool: error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
