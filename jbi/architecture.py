"""The architecture graph and the `architecture.sync` hook.

`initial` builds the first graph from the PRD. `sync` is a pure function: it takes a
graph and the plan and returns a new graph. It never calls a model. Most events change
only statuses. An agent can also propose a structural change in a patch; a patch that
fails any check is rejected whole.

Patch schema (every key is optional):

    {"add_nodes":    [{"id", "name", "kind", "layer", "tech", "notes"}],
     "add_edges":    [{"from", "to", "label", "protocol", "id"}],
     "update_nodes": [{"id", "name", "kind", "layer", "tech", "notes"}],
     "remove_edges": ["edge-id"]}
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .protocol import TASK_ID

KINDS = ("client", "service", "worker", "database", "cache", "queue", "storage", "external")
BUILDING_TASKS = ("running", "verifying", "queued", "merging")
NODE_FIELDS = ("name", "kind", "layer", "tech", "notes")
PATCH_KEYS = ("add_nodes", "add_edges", "update_nodes", "remove_edges")


@dataclass(frozen=True)
class SyncResult:
    changed: bool
    graph: dict
    summary: str


def _node(spec: dict) -> dict:
    return {"id": spec["id"], "name": spec.get("name") or spec["id"], "kind": spec.get("kind", "service"),
            "layer": spec.get("layer", ""), "tech": spec.get("tech", ""), "status": "planned",
            "tasks": [], "notes": spec.get("notes", "")}


def _edge(spec: dict, edge_id: str) -> dict:
    return {"id": edge_id, "from": spec["from"], "to": spec["to"], "label": spec.get("label", ""),
            "protocol": spec.get("protocol", ""), "status": "planned"}


def _edge_id(edges: dict, source: str, target: str) -> str:
    base, number = f"{source}--{target}", 1
    edge_id = base
    while edge_id in edges:
        number += 1
        edge_id = f"{base}-{number}"
    return edge_id


def initial(prd: dict, plan: dict) -> dict:
    """The first graph: one node per PRD component, one edge per `talks_to` entry."""
    nodes = {c["id"]: _node(c) for c in prd.get("components", [])}
    edges: dict[str, dict] = {}
    for component in prd.get("components", []):
        for link in component.get("talks_to", []):
            if link.get("to") in nodes:
                spec = {"from": component["id"], **link}
                edge_id = _edge_id(edges, spec["from"], spec["to"])
                edges[edge_id] = _edge(spec, edge_id)
    graph = {"revision": 1, "nodes": nodes, "edges": edges}
    _link_tasks(graph, plan)
    return graph


def _link_tasks(graph: dict, plan: dict) -> None:
    linked: dict[str, list[str]] = {node_id: [] for node_id in graph["nodes"]}
    tasks = plan.get("tasks", {})
    for tid in plan.get("order", list(tasks)):
        for component in tasks[tid].get("components", []):
            if component in linked:
                linked[component].append(tid)
    for node_id, node in graph["nodes"].items():
        node["tasks"] = linked[node_id]


# ---- status ----------------------------------------------------------------------------


def _node_status(node: dict, tasks: dict, current: str) -> str:
    states = [tasks[t]["status"] for t in node["tasks"] if t in tasks]
    if not states:
        return current
    if all(s == "merged" for s in states):
        return "built"
    if any(s in BUILDING_TASKS for s in states):
        return "building"
    if any(s == "failed" for s in states):
        return "failed"
    return "planned"


def _edge_status(edge: dict, nodes: dict) -> str:
    ends = {nodes[edge["from"]]["status"], nodes[edge["to"]]["status"]}
    if ends == {"built"}:
        return "built"
    if "failed" in ends:
        return "failed"
    return "building" if "building" in ends else "planned"


def _refresh_status(graph: dict, plan: dict) -> None:
    _link_tasks(graph, plan)
    tasks = plan.get("tasks", {})
    for node in graph["nodes"].values():
        node["status"] = _node_status(node, tasks, node["status"])
    for edge in graph["edges"].values():
        edge["status"] = _edge_status(edge, graph["nodes"])


# ---- patch -----------------------------------------------------------------------------


def _check_fields(spec: object, where: str, errors: list[str]) -> bool:
    if not isinstance(spec, dict):
        errors.append(f"{where} must be an object")
        return False
    if "kind" in spec and spec["kind"] not in KINDS:
        errors.append(f"{where} has kind {spec['kind']!r}; use one of {', '.join(KINDS)}")
    for key in NODE_FIELDS:
        if key in spec and not isinstance(spec[key], str):
            errors.append(f"{where} field {key} must be a string")
    return True


def _patch_errors(graph: dict, patch: object) -> list[str]:
    if not isinstance(patch, dict):
        return ["the patch must be an object"]
    errors = [f"unknown patch key {key!r}" for key in patch if key not in PATCH_KEYS]
    for key in PATCH_KEYS:
        if not isinstance(patch.get(key, []), list):
            errors.append(f"{key} must be a list")
    if errors:
        return errors
    node_ids = set(graph["nodes"])
    for index, spec in enumerate(patch.get("add_nodes", [])):
        where = f"add_nodes[{index}]"
        if _check_fields(spec, where, errors):
            if not isinstance(spec.get("id"), str) or not TASK_ID.match(spec["id"]):
                errors.append(f"{where} needs an id that matches {TASK_ID.pattern}")
            elif spec["id"] in node_ids:
                errors.append(f"{where} id {spec['id']!r} already exists")
            else:
                node_ids.add(spec["id"])
    for index, spec in enumerate(patch.get("update_nodes", [])):
        where = f"update_nodes[{index}]"
        if _check_fields(spec, where, errors) and spec.get("id") not in node_ids:
            errors.append(f"{where} names unknown node {spec.get('id')!r}")
    edge_ids = set(graph["edges"])
    for index, edge_id in enumerate(patch.get("remove_edges", [])):
        if edge_id not in edge_ids:
            errors.append(f"remove_edges[{index}] names unknown edge {edge_id!r}")
    edge_ids -= set(patch.get("remove_edges", []))
    for index, spec in enumerate(patch.get("add_edges", [])):
        where = f"add_edges[{index}]"
        if not isinstance(spec, dict):
            errors.append(f"{where} must be an object")
        elif spec.get("from") not in node_ids or spec.get("to") not in node_ids:
            errors.append(f"{where} connects unknown node {spec.get('from')!r} -> {spec.get('to')!r}")
        elif spec.get("id") in edge_ids:
            errors.append(f"{where} id {spec['id']!r} already exists")
        elif spec.get("id") is not None:
            edge_ids.add(spec["id"])
    return errors


def _apply_patch(graph: dict, patch: dict) -> None:
    for edge_id in patch.get("remove_edges", []):
        del graph["edges"][edge_id]
    for spec in patch.get("add_nodes", []):
        graph["nodes"][spec["id"]] = _node(spec)
    for spec in patch.get("update_nodes", []):
        graph["nodes"][spec["id"]].update({k: spec[k] for k in NODE_FIELDS if k in spec})
    for spec in patch.get("add_edges", []):
        edge_id = spec.get("id") or _edge_id(graph["edges"], spec["from"], spec["to"])
        graph["edges"][edge_id] = _edge(spec, edge_id)


def _describe_changes(before: dict, after: dict) -> str:
    parts = []
    for node_id, node in after["nodes"].items():
        old = before["nodes"].get(node_id)
        if old is None:
            parts.append(f"{node_id} added")
        elif old["status"] != node["status"]:
            parts.append(f"{node_id} {node['status']}")
        elif old != node:
            parts.append(f"{node_id} updated")
    parts += [f"{node_id} removed" for node_id in before["nodes"] if node_id not in after["nodes"]]
    edge_delta = len(after["edges"]) - len(before["edges"])
    if edge_delta:
        parts.append(f"{abs(edge_delta)} edge{'s' if abs(edge_delta) != 1 else ''} {'added' if edge_delta > 0 else 'removed'}")
    return "Architecture: " + (", ".join(parts) or "edges updated")


def sync(graph: dict, plan: dict, *, event: str, task: str | None, patch: dict | None) -> SyncResult:
    """Return the graph after `event`. `changed` is True only when the graph differs."""
    updated, notes = copy.deepcopy(graph), []
    if patch:
        errors = _patch_errors(updated, patch)
        if errors:
            notes.append("patch rejected: " + "; ".join(errors))
        else:
            _apply_patch(updated, patch)
    _refresh_status(updated, plan)
    changed = {k: v for k, v in updated.items() if k != "revision"} != \
              {k: v for k, v in graph.items() if k != "revision"}
    if not changed:
        return SyncResult(False, graph, "; ".join(notes) or f"Architecture unchanged after {event}")
    updated["revision"] = graph["revision"] + 1
    return SyncResult(True, updated, "; ".join([_describe_changes(graph, updated), *notes]))
