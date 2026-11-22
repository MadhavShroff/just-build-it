# Standalone design

## Contracts

`Tracking(path)` opens one SQLite database with WAL, foreign keys, and `BEGIN IMMEDIATE` writes. The default is the run directory's `fleet.db`, not the event database. The store keeps its own records and imports none.

`Tracking.create_task(title, goal="", owns, branch, worktree, base_commit, task_id=None, node=None) -> task`.
`Tracking.dispatch(task_id, session_id, owns, branch, worktree) -> dispatch record`.
`Tracking.authorize_write(task_id, path, tool_write=False) -> None or LeaseError`.
`Tracking.release(task_id, reason, abandon=False) -> release record`.
`Tracking.fleet() -> one JSON-compatible dict`.
`Tracking.send(target, text, sender="founder") -> list of message records`.

Tasks hold id, title, goal, owns JSON, branch, worktree, base commit, state, node, timestamps, and release reason. Dispatches hold id, task, session, attempt, model, continuation flag, brief digest, timestamps, receipt, and verdict. Sessions hold id, node, history, status, and reset timestamps. Messages hold delivery state in their own table.

Leases hold task, owns JSON, branch, worktree, base commit, granted timestamp, released timestamp, and release reason. CI submissions hold task, PR, pinned head, base, service reply, green run, merge SHA, timestamps, and state. Merges hold task, PR, verified head, squash SHA, green-run evidence, and timestamp. Messages hold id, sender, target, text, timestamp, delivery state, and service receipt. Costs hold dispatch, input, output, cache-read, cache-write, reported cost, and timestamp. A missing provider field is null. The cache rate divides reads by reads, cache writes, and uncached input. A zero denominator gives null.

## Lease model

The design reuses Rig's conservative path model. Path normalization rejects absolute owns entries, traversal, empty entries, and a trailing slash interpretation change. A plain path owns itself. A directory prefix owns its descendants. `fnmatch` handles explicit globs. A conflict compares each literal prefix before a wildcard. Every possible overlap refuses. The refusal names the task, session, and path holder. One live lease exists per task.

Dispatch writes the task, session, dispatch, and lease in one transaction. Agent startup never happens before commit. A later stage appends a dispatch under the same lease. A renewal does not expand owns paths or grant another live lease. No lease means no tool write. claude-mux-lite fences dedicated writes and shell mutations through the same lease. The fence blocks `git stash`, which can lose common repository state. A non-leashed jbi-owned receipt and scratch path are separate capabilities. The dispatcher checks all changed paths before committing.

Release first checks merged or abandoned state. A clean leased worktree resets to the recorded base commit and becomes detached. The branch remains parked under a timestamped `jbi-parked/` ref. Untracked state is not silently discarded; the reset refuses while Git would preserve it. Any dirty, detached-mismatch, or untracked state refuses release. The lease stays live on refusal. The dispatcher commits the release only after parking succeeds. History remains in SQLite.

## Provider-neutral claude-mux-lite

The new package contains tools, a request builder, a raw HTTP transport, a session loop, checkpointing, parsing, and tests. Its code derives selectively from the MIT-licensed rswarm harness. It imports no rswarm modules. It requires only the Python standard library. Provider credentials come only from `CLAUDE_MUX_BASE_URL` and `CLAUDE_MUX_API_KEY`, or the configured endpoint injection in tests. Files and logs never contain credentials.

The tool schemas cover bash, file reads, file writes, exact edits, directory lists, and regex searches. Path resolution rejects workspace escapes, symlink escapes, and unleased writes. Linux runs bash under `bwrap`, with a read-only root and writable worktree, Git storage, and scratch directory. A real write outside those leaves refuses. A process cannot reach the temporary space of another task. No live shell runs without `bwrap`. `git status` detects repository mutations after each tool call. The fence restores the path from the base commit, records the refusal, and fails the tool call. Each response appends complete content blocks to session history. An identical system prompt and ordered tool list precede messages. Explicit ephemeral cache breakpoints mark stable content and growing history.

The provider endpoint uses `POST {base}/v1/messages`. The request specifies model, maximum output, effort, tools, system messages, and message history. Every `tool_use` gets one result. A missing usage field stays null. Parser failures return a recorded transport failure. A deterministic fake endpoint verifies every request and usage mapping without network access.

## Cache method

A brief puts the repository map and frozen contracts first. Its task goal, attempt, owns paths, acceptance, findings, and receipt instructions come last. The header has no timestamp or random identifier. Each task starts a fresh session. A same-task correction continues that task's history. Every dispatch records `continuation` as a boolean. The provider cache serves the identical node prefix inside its TTL. The dispatcher runs same-node tasks consecutively. A released lease has no continuation write authority. Cost records distinguish one dispatch from a whole session. The fleet read reports dispatches with reads, writes, uncached input, and hit rate.

This request shape follows the raw HTTP protocol accepted by claude-mux-lite. The implementation does not change the prescribed neutral provider boundary to an Anthropic SDK.

## Messages and CI

`message.send` validates a session id or `--all`. The runtime persists one message per target. claude-mux-lite injects pending text before a model call and marks delivery. An idle session receives the next task's message without restoring task history. The fleet read shows delivery state. It never claims agent action. `just-build-it send --agent ID --text TEXT` addresses one agent. `--all --text TEXT` addresses all sessions.

`CIAdapter` invokes `ci-conductor ready PR --head SHA` directly. No interface-llm wrapper exists in jbi. The adapter validates textual or structured replies and requires active-mode acceptance. A direct `job` read records pending until a terminal run. That run supplies a green verdict and squash SHA for the same head. A missing squash SHA never counts as merged. The fleet read combines internal CI records with exact service receipts.

## Slice verification

Static AST imports enumerate all external Python imports. Package-source scanning fails on external fleet tooling, store names, and their path conventions. Ordinary local decisions and variable names do not trigger the scanner.

The lease mutant drops ownership matching or conflict checks. The release mutant parks a dirty tree or drops the base-commit guard. The cache mutant swaps stable and task sections or drops usage fields. The CI mutant accepts a green verdict without a verified squash SHA. Each mutant lives in an archive copy. The healthy code must pass while the mutant test fails. Offline service fakes prove direct invocations, fan-out, and cache accounting.