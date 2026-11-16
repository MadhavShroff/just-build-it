# just-build-it DAG Format Specification

This document is the normative specification for task DAG files that `just-build-it`, `jbi/dag_loader.py`, `jbi/waves.py`, and `jbi/dag_tool.py` accept.

The loader validates file syntax, schema fields, identifier syntax, owned paths, wave groups, and dependency cycles before dispatch. If any check fails, the loader raises `jbi.dag_loader.Invalid` (`409 conflict`) and dispatches nothing.

---

## 1. Accepted DAG Shapes

`jbi.dag_loader.load` and `just-build-it --dag <path>` accept two deterministic shapes:

1. **Restricted YAML (`.yaml` / `.yml`)** — parsed and validated by `/home/madhavshroff/src/devloop-cli/wave-compiler`.
2. **Planning JSON (`dag.json`)** — `.planning/.../dag.json` objects containing a `nodes` or `tasks` list, with optional per-node `subtasks` or `batches` expansion.

---

## 2. Field Reference

### 2.1 Root Fields

| Field | Shape | Type | Required | Description |
|---|---|---|---|---|
| `project` | YAML, JSON | string | YAML: yes; JSON: optional (defaults to `"dag"`) | Project name for the run (`run.name`). |
| `repo` | YAML, JSON | string | YAML: yes; JSON: optional (defaults to `"."`) | Target repository path (`run.target`). |
| `mode` | YAML, JSON | `"new"` \| `"existing"` | YAML: yes; JSON: optional (defaults to `"existing"`) | Repository initialization mode. |
| `waves` | YAML, JSON | `"auto"` \| `"explicit"` (YAML) or `list[list[str]]` (JSON) | YAML: yes; JSON: optional | Wave assignment mode or explicit wave list. |
| `wave_groups` | YAML, JSON | `list[list[str]]` | Required when `waves == "explicit"` | Explicit ordered wave groups of task identifiers. |
| `tasks` | YAML, JSON | `list[object]` | Required in YAML; alternative to `nodes` in JSON | Ordered list of task definitions. |
| `nodes` | JSON | `list[object]` | Required in `.planning` `dag.json` when `tasks` is absent | Ordered list of planning node definitions. |
| `completed_deps` | JSON | `list[str]` | Optional | Upstream node or task IDs already satisfied by earlier waves. Recorded on `task.upstream_deps` during single-wave compilation. |

### 2.2 Task and Node Fields

| Field | Shape | Type | Required | Description |
|---|---|---|---|---|
| `id` | YAML, JSON | string | Yes | Unique task identifier matching `^[A-Za-z][A-Za-z0-9_-]{0,39}$`. |
| `title` | YAML, JSON | string | Yes | Non-empty human-readable task title. |
| `goal` | YAML, JSON | string | YAML: yes; JSON: derived from `title`, `path`, `spec` when omitted | Non-empty task goal written to the agent brief. |
| `deps` | YAML, JSON | `list[str]` | Yes (defaults to `[]` in JSON nodes) | Task IDs that must merge before this task starts. |
| `acceptance` | YAML, JSON | string | YAML: yes; JSON: defaults to `"true"` when omitted | Shell command that must exit `0` during the acceptance stage. |
| `owns` | YAML, JSON | `list[str]` | Optional (defaults to `[]`) | Relative file paths, directory prefixes, or globs that this task may edit. |
| `components` | JSON | `list[str]` | Optional (defaults to `[]`) | Architecture graph component IDs touched by this task. |
| `owner` | JSON | string | Optional | Slot or worker assigned to the node (for example `"work13"`). |
| `path` | JSON | string | Optional | Relative path to the node `PLAN.md` document. |
| `spec` | JSON | string | Optional | Relative path to the node `SPEC.md` document. |
| `deliverable` | JSON | string | Optional | Deliverable anchor ID (for example `"d03-d1"`). |
| `node` | JSON | string | Optional | Parent launch node ID when the task is an expanded batch (for example `"D03"`). |
| `batch` | JSON | string | Optional | Batch identifier within the parent node (for example `"B1"`). |
| `effort` | JSON | string | Optional | Estimated effort tier (`"XS"`, `"S"`, `"M"`, `"L"`, `"XL"`). |
| `eta_s` | JSON | integer | Optional | Explicit estimated duration in seconds. |
| `spec_details` | JSON | `list[str]` \| string | Optional | Self-contained specification context, frame corrections, and founder decisions for smaller models. |
| `contracts` | JSON | `list[str]` | Optional | Exact interfaces, ports, collections, indexes, and HTTP routes implemented in this batch. |
| `invariants` | JSON | `list[str]` | Optional | Security, transaction, authority, and failure-handling invariants enforced in this batch. |
| `test_cases` | JSON | `list[str]` | Optional | EARS test case IDs and required assertions covered by this batch. |
| `done_criteria` | JSON | `list[str]` | Optional | Explicit completion checklist and forbidden shortcuts for this batch. |
| `subtasks` / `batches` | JSON | `list[object]` | Optional | Ordered child batch tasks inside a parent node. |

### 2.3 Subtask / Batch Fields (`subtasks` or `batches`)

A node in `dag.json` may carry its ordered batches under `"subtasks"` or `"batches"`. Each subtask object supports:

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | Yes | Subtask identifier (for example `"D03-B1"` or `"B1"`). |
| `title` | string | Yes | Non-empty subtask title. |
| `deps` | `list[str]` | Yes | Intra-node or cross-node dependencies for this subtask. |
| `owns` | `list[str]` | Optional | Owned relative paths for this subtask (inherits parent `owns` when omitted). |
| `acceptance` | string | Optional | Acceptance command (inherits parent `acceptance` or `"true"` when omitted). |
| `goal` | string | Optional | Subtask goal (defaults to `title` when omitted). |
| `batch` | string | Optional | Short batch code (for example `"B1"`). |
| `effort` | string | Optional | Effort tier (`"XS"`, `"S"`, `"M"`, `"L"`, `"XL"`). |
| `eta_s` | integer | Optional | Estimated duration in seconds. |

**Subtask expansion rule:**
When `jbi.dag_loader` loads a node with `subtasks` or `batches`, it expands that node into its child tasks before wave compilation:
1. Every child task inherits the parent node's `deps` in addition to its own `deps` (`deps = parent.deps + subtask.deps`).
2. Local batch references in `subtask.deps` (such as `"B1"` inside node `"D03"`) resolve to the expanded task ID (`"D03-B1"`).
3. Any downstream node that depended on the parent node ID has that dependency rewritten to the expanded child task IDs.
4. The wave compiler then arranges all expanded child tasks into topological waves for maximum parallelism.

---

## 3. Validation Rules and Refusals

The loader validates every DAG before dispatch and refuses the input when any rule below is violated:

1. **Identifier rule (`id`)**:
   - Must start with an ASCII letter (`A-Z` or `a-z`) and contain only letters, digits, `_`, or `-`, up to 40 characters (`^[A-Za-z][A-Za-z0-9_-]{0,39}$`).
   - Duplicate task IDs are refused (`duplicate task id '<id>'`).
2. **Required non-empty strings**:
   - Root `project`, `repo`, `mode`, `waves` and task `id`, `title`, `goal`, `acceptance` must be non-empty strings.
   - `mode` must be `"new"` or `"existing"`.
   - `waves` must be `"auto"` or `"explicit"`.
3. **Dependency rules (`deps`)**:
   - Every dependency must name an existing task ID (`depends on unknown task '<dep>'`).
   - Self-dependencies are refused (`task '<id>' depends on itself`).
   - Repeated dependencies on the same task are refused (`repeats a dependency`).
4. **Cycle refusal**:
   - Any cycle in `deps` is refused with the closed cycle path in task order:
     `dependency cycle: <t1> -> <t2> -> ... -> <t1>`.
5. **Owned path rules (`owns`)**:
   - Every entry must be a non-empty relative POSIX path.
   - Leading `/`, backslashes `\`, parent traversal segments `..`, and duplicate entries are refused (`owns invalid path '<path>'`).
6. **Explicit wave group rules (`wave_groups`)**:
   - Allowed only when `waves == "explicit"`.
   - Every group must be a non-empty list of known task IDs, each task must appear in exactly one group, and every dependency of a task in group `N` must appear in a group `< N`.
7. **Restricted-YAML syntax rules**:
   - Tabs and `#` comments are forbidden.
   - Scalar values must be JSON double-quoted strings; list values must be JSON flow lists of strings (`["a", "b"]`).

---

## 4. Worked Examples

### 4.1 Restricted YAML Example

```yaml
project: "demo"
repo: "./demo"
mode: "existing"
waves: "auto"
tasks:
  - id: "D01"
    title: "Build core contracts"
    goal: "Define canonical data contracts"
    deps: []
    acceptance: "python3 -m unittest"
    owns: ["src/contracts.py"]
  - id: "D02"
    title: "Build storage service"
    goal: "Persist validated records"
    deps: ["D01"]
    acceptance: "python3 -m unittest"
    owns: ["src/storage.py"]
```

### 4.2 Planning `dag.json` Example with `subtasks`

```json
{
  "schemaVersion": 1,
  "project": "tangram-v1",
  "repo": "/home/madhavshroff/tangram-workspace",
  "mode": "existing",
  "nodes": [
    {
      "id": "D02",
      "title": "Native storage, access, revisions and events",
      "deps": [],
      "owner": "work15",
      "path": "04-tables-docs-slides-charts/D02-PLAN.md",
      "spec": "04-tables-docs-slides-charts/D02-SPEC.md",
      "deliverable": "d02-d1",
      "owns": ["tangram-backend/nest-backend/src/platform-direct/native/**"],
      "acceptance": "npx tsc --noEmit"
    },
    {
      "id": "D03",
      "title": "Shared Artifact core and Library identity",
      "deps": ["D02"],
      "owner": "work13",
      "path": "04-tables-docs-slides-charts/D03-PLAN.md",
      "spec": "04-tables-docs-slides-charts/D03-SPEC.md",
      "deliverable": "d03-d1",
      "subtasks": [
        {
          "id": "D03-B1",
          "batch": "B1",
          "title": "D01 imports, pure record validation, indexes, source policy ports",
          "deps": [],
          "owns": ["tangram-backend/nest-backend/src/platform-direct/artifacts/storage/**"],
          "acceptance": "node scripts/verify-launch-artifact-core.mjs --require-cases 16"
        },
        {
          "id": "D03-B2",
          "batch": "B2",
          "title": "Atomic source publication, Library pointer, scoped authority",
          "deps": ["D03-B1"],
          "owns": ["tangram-backend/nest-backend/src/platform-direct/artifacts/artifact-core.service.ts"],
          "acceptance": "node scripts/verify-launch-artifact-core.mjs --require-cases 16"
        }
      ]
    }
  ]
}
```
