---
name: dev-pipeline
description: Use the three local development pipeline services (interface-llm, ci-conductor, and just-build-it) to build, validate, preview, and execute task DAGs end to end. Trigger on DAG creation, dag_tool edits, ceremony runs, preview dashboards, or end-to-end wave execution.
---

# dev-pipeline

Use `interface-llm`, `ci-conductor`, and `just-build-it` together to compile task DAGs into waves, dispatch agent work across isolated worktrees, run deterministic reviews and verification, and land PRs through the CI train.

## 1. The Three Services

| Service | CLI / Endpoint | Role and Affordances |
|---|---|---|
| `interface-llm` (`illm`) | `illm services`, `illm help <service>`, `illm call <service> <verb>`, `~/.local/share/interface-llm/mailbox/<session>.jsonl` | Local service bus and session mailbox. Delivers asynchronous `interface-llm notice` events (such as `ci-conductor` PR verdicts) to agent sessions without polling loops. |
| `ci-conductor` | `illm call ci-conductor submit --pr <N> --branch <B> --sha <SHA> --session <S>` (or `/home/madhavshroff/src/ci-conductor/bin/ci-submit`) | Admissions-guarded CI and merge train. Runs verification lanes against submitted draft PRs, records train history in `~/.local/state/ci-train/history.jsonl`, squash-merges green PRs, and posts completion notices to the caller's `illm` mailbox. |
| `just-build-it` (`jbi`) | `just-build-it`, `python3 -m jbi`, `python3 -m jbi.ceremony`, `python3 -m jbi.dag_tool` | Deterministic DAG wave compiler, multi-stage supervisor (`implement`, `acceptance`, `verify`, 4-way `review-cascade` + `review-ds-1..3`, `security`, `test-audit`, `methodology-audit`, `dry-run`, `merge`), ceremony runner, and no-LLM metrics + DAG preview web UI. Runs as user service `just-build-it.service`. |

---

## 2. Normative DAG Format (`docs/DAG-FORMAT.md`)

Full specification: `/home/madhavshroff/src/just-build-it/docs/DAG-FORMAT.md`.

`jbi.dag_loader.load` accepts two shapes and validates both before dispatch:

1. **Restricted YAML (`.yaml` / `.yml`)** — validated by `/home/madhavshroff/src/devloop-cli/wave-compiler`:
   - Root keys: `project`, `repo`, `mode` (`"new"` | `"existing"`), `waves` (`"auto"` | `"explicit"`), optional `wave_groups`, and `tasks`.
   - Task keys: `id`, `title`, `goal`, `deps`, `acceptance`, `owns`.
2. **Planning JSON (`dag.json`)** — `.planning/.../dag.json`:
   - Root keys: `project`, `repo`, `mode`, `waves` / `wave_groups`, `completed_deps` (optional satisfied upstream IDs), and `nodes` or `tasks`.
   - Node/task keys: `id`, `title`, `goal`, `deps`, `owns`, `acceptance`, `components`, `owner`, `path`, `spec`, `deliverable`, `node`, `batch`, `effort`, `eta_s`, and optional `subtasks` / `batches`.
   - When a node contains `subtasks` or `batches`, `jbi.dag_loader` expands each subtask into a child task whose `deps` include the parent node's `deps`, rewrites downstream node references, and compiles all tasks into topological waves for maximum parallelism.

**Refusal rules:**
- Task IDs must match `^[A-Za-z][A-Za-z0-9_-]{0,39}$` and be unique.
- Owned paths must be relative POSIX paths without leading `/`, `\`, or `..`.
- Unknown dependencies, self-dependencies, duplicate dependencies, and dependency cycles are refused with `409 conflict`.

---

## 3. Constructing and Editing DAGs with `jbi.dag_tool`

Every mutating command validates the edited DAG before writing to disk and refuses to write an invalid DAG.

```bash
# 1. Create a new DAG file with an initial task
python3 -m jbi.dag_tool new /tmp/feature-dag.json \
  --project tangram-v1 --repo /home/madhavshroff/tangram-workspace \
  --mode existing --waves auto \
  --id D01 --title "Native table contracts" --acceptance "npx tsc --noEmit"

# 2. Add a task with owned paths and dependencies
python3 -m jbi.dag_tool add-task /tmp/feature-dag.json \
  --id D03 --title "Shared Artifact core" \
  --deps D01 --owns "tangram-backend/nest-backend/src/platform-direct/artifacts/**" \
  --acceptance "node tangram-backend/nest-backend/scripts/verify-launch-artifact-core.mjs"

# 3. Add a dependency edge between existing tasks
python3 -m jbi.dag_tool add-dep /tmp/feature-dag.json --task D03 --dep D01

# 4. Add ordered batch subtasks to a node
python3 -m jbi.dag_tool add-subtask /tmp/feature-dag.json \
  --node D03 --id D03-B1 --batch B1 \
  --title "D01 imports and pure record validation" \
  --owns "tangram-backend/nest-backend/src/platform-direct/artifacts/storage/**"

python3 -m jbi.dag_tool add-subtask /tmp/feature-dag.json \
  --node D03 --id D03-B2 --batch B2 \
  --title "Atomic source publication and Library pointer" \
  --deps D03-B1 \
  --owns "tangram-backend/nest-backend/src/platform-direct/artifacts/artifact-core.service.ts"

# 5. List subtasks on a node
python3 -m jbi.dag_tool list-subtasks /tmp/feature-dag.json --node D03

# 6. Assign explicit wave groups (only when waves are not auto-expanded)
python3 -m jbi.dag_tool set-wave /tmp/feature-dag.json --wave 2 --tasks D03

# 7. Remove a task
python3 -m jbi.dag_tool remove-task /tmp/feature-dag.json --id D03 --prune-deps

# 8. Validate and inspect waves, parallelism, critical path, and ETAs
python3 -m jbi.dag_tool validate /tmp/feature-dag.json
python3 -m jbi.dag_tool show /tmp/feature-dag.json
```

---

## 4. End-to-End Session Workflow

1. **Author and validate the DAG**:
   Build or update the DAG file with `python3 -m jbi.dag_tool` and verify topological waves and critical path with:
   ```bash
   python3 -m jbi.dag_tool validate <dag-file>
   python3 -m jbi.dag_tool show <dag-file>
   ```
2. **Preview the execution graph and live ETAs**:
   Run the deterministic dry-run preview server:
   ```bash
   just-build-it --dag <dag-file> --dry-run --preview --target <repo>
   ```
   Open the printed localhost/Tailscale URL and switch to the **DAG Preview** tab to inspect wave columns, parallel badges, critical-path chain, and per-task/per-wave ETAs.
3. **Execute the DAG or run the deterministic ceremony**:
   - Full pipeline supervisor:
     ```bash
     just-build-it --dag <dag-file> --rigor high --target <repo>
     ```
   - Window-pool ceremony dispatcher (with `ci-conductor` and `illm` notice wakeup):
     ```bash
     python3 -m jbi.ceremony --dag <dag-file> --session <slot>
     ```
   - Resume the latest durable SQLite run after a restart:
     ```bash
     just-build-it resume --if-exists
     ```
