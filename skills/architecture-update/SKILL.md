---
name: architecture-update
description: Propose a change to the architecture graph with .jbi/architecture.patch.json when your task adds, renames or removes a component or a link.
---

# Update the architecture

The system draws a graph of components and links. Status changes follow the tasks and need no action from you. Write a patch only when the structure changes.

## When to write a patch
- Your task adds a service, client, database, queue, cache, storage or external system.
- Your task adds a new link between two components.
- A component changes its technology, name or role.
- A link is removed.

Write no patch when you only finish work on a planned component.

## How
1. Write `.jbi/architecture.patch.json` in your worktree, as one JSON object.
2. Commit it with your task.
3. The system validates the patch after the merge. It rejects the whole patch on any error.

## Schema
All keys are optional.

```json
{
  "add_nodes":    [{"id": "cache", "name": "Cache", "kind": "cache", "layer": "data", "tech": "Redis", "notes": ""}],
  "add_edges":    [{"from": "api", "to": "cache", "label": "reads", "protocol": "tcp"}],
  "update_nodes": [{"id": "api", "tech": "FastAPI"}],
  "remove_edges": ["api--db"]
}
```

- `kind` is one of: client, service, worker, database, cache, queue, storage, external.
- A new `id` matches `^[a-z][a-z0-9-]{0,39}$` and is not in the graph.
- An edge names two nodes that exist, or that the same patch adds.
- `remove_edges` and `update_nodes` name ids that exist.
