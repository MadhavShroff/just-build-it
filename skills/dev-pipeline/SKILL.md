---
name: dev-pipeline
description: Use interface-llm, ci-conductor, and just-build-it to compile, contextualize, verify, preview, and execute task DAGs from planning docs.
---

# dev-pipeline

Use `interface-llm`, `ci-conductor`, and `just-build-it` (`jbi`) to compile task DAGs into waves, dispatch bounded briefs to isolated worktrees, run deterministic reviews, and land PRs through the CI train.

## 1. The Three Services
- `interface-llm` (`illm`): Local service bus and session mailbox (`illm services`, `illm help <service>`, `illm call <service> <verb>`, `~/.local/share/interface-llm/mailbox/<session>.jsonl`). Delivers event-driven `interface-llm notice` completions without polling.
- `ci-conductor`: Admissions-guarded CI and merge train (`illm call ci-conductor submit --pr <N> --branch <B> --sha <SHA> --session <S>`). Runs verification lanes, records `~/.local/state/ci-train/history.jsonl`, squash-merges green PRs, and posts completion notices to the caller's `illm` mailbox.
- `just-build-it` (`jbi`): Deterministic wave compiler, multi-stage supervisor (`implement`, `acceptance`, `verify`, 4-way `review-cascade` + `review-ds-1..3`, `security`, `test-audit`, `methodology-audit`, `dry-run`, `merge`), ceremony runner (`python3 -m jbi.ceremony`), and no-LLM metrics + DAG preview UI (`--preview`).

## 2. DAG Format and `jbi.dag_tool` (`docs/DAG-FORMAT.md`)
`jbi.dag_loader.load` validates restricted YAML (`.yaml`) and planning JSON (`dag.json`) before dispatch and refuses invalid IDs, unsafe `owns` paths, unknown `deps`, or cycles (`409 conflict`).
- Construct and edit DAGs with `python3 -m jbi.dag_tool`:
  - `new <file> --project <P> --repo <R> --mode existing|new --waves auto|explicit [--id <ID> --title <T>]`
  - `add-task <file> --id <ID> --title <T> --goal <G> --deps <D> --owns <PATHS> --acceptance <CMD> [--effort S|M|L]`
  - `add-dep <file> --task <ID> --dep <DEP>`
  - `add-subtask <file> --node <NODE> --id <SUB_ID> --batch <B> --title <T> --deps <D> --owns <PATHS>`
  - `list-subtasks <file> [--node <NODE>] [--json]`
  - `set-wave <file> --wave <N> --tasks <ID1,ID2>`
  - `remove-task <file> --id <ID> [--prune-deps]`
  - `validate <file> [--json]` and `show <file> [--json]`

## 3. Standing Rule: Compiling Execution DAGs from Planning Docs
When building an execution DAG from `.planning` `PLAN.md` and `SPEC.md` documents, follow these mandatory rules so smaller, simpler models can implement every task without guessing:

1. **Zero-Omission Batch Extraction**:
   - Read every node's `PLAN.md` and `SPEC.md` end to end, including all ordered batches (and sub-batches such as `B4a`), frame corrections, amendments, and founder card rulings (`tpv1-planning-*`, `integration-*`).
   - Do not invent or merge batches. Every batch in the `PLAN.md` becomes its own task (`<NODE>-<BATCH>`).
2. **Self-Contained Task Contextualization for Smaller Models**:
   - Never write a one-line summary as the task specification. Populate every task with complete, self-contained fields:
     - `goal`: Multi-sentence technical specification naming the exact files, classes, functions, ports, entities, routes, state transitions, and error codes to build.
     - `spec_details`: Upstream deliverables consumed, downstream consumers, frame corrections, and founder card answers that modify earlier sections of the plan.
     - `contracts`: Exact TypeScript/Python interfaces, port signatures, MongoDB/SQLite collections, index tuples, and HTTP/gRPC endpoints for that batch.
     - `invariants`: Transaction boundaries, CAS/generation fences, authority checks, and forbidden shortcuts (for example, no local outbox/receipt duplicates where shared E01/D02 stores apply).
     - `test_cases`: Every EARS test case ID (`<NODE>-Tnn`) assigned to the batch with its trigger and required assertions.
     - `done_criteria`: Explicit exit checklist for the batch.
     - `owns`: Complete list of existing and proposed relative file paths touched by the batch (zero omitted files).
     - `acceptance`: Exact executable verification command that exits `0` only when the batch passes.
3. **Dependency and Maximum-Parallelism Rules**:
   - Batch `N` of a node depends on batch `N-1` of the same node.
   - Batch `1` of a node depends on the parent node's `deps` and on all batch tasks of any upstream wave nodes it depends on.
   - Independent batches across nodes must be placed in the same wave for maximum parallelism.
4. **Mandatory Verification Audit Before Handoff**:
   - Run a verification pass comparing the generated DAG against every source `PLAN.md`:
     - Verify every batch, every file in the plan's file table, every EARS test case (`T01..Tnn`), and every frame correction/amendment is present in the DAG.
     - Run `python3 -m jbi.dag_tool validate <dag-file>` and `python3 -m jbi.dag_tool show <dag-file>` and confirm zero validation errors.
