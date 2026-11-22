# just-build-it — standalone specification

2026-11-22. jbi turns one brief into verified, merged code. Internal tracking controls every step. The package reads and writes no board state, slot registry, ownership registry, or external task store.

## Requirements

The founder ordered dispatch-time ownership on 2026-11-22. The founder ordered standalone jbi on the same date. The second order replaces board-backed tracking.

- WHEN jbi dispatches a task THE SYSTEM SHALL record the task, session, owns paths, branch, worktree, and base commit in SQLite.
- WHEN jbi dispatches a task THE SYSTEM SHALL grant a lease before agent execution.
- WHEN paths overlap a live lease THE SYSTEM SHALL refuse dispatch and name the holder.
- WHEN an agent writes THE SYSTEM SHALL allow only leased paths, the jbi scratch directory, and the receipt path.
- WHEN a task merges or is abandoned THE SYSTEM SHALL park the branch, reset the worktree, and release the lease.
- WHEN claude-mux-lite dispatches a fresh task THE SYSTEM SHALL use a shared cached node prefix, with task content last.
- WHEN claude-mux-lite dispatches a same-task correction THE SYSTEM SHALL append the correction to that task's history.
- THE SYSTEM SHALL place stable brief content first and task content last.
- THE SYSTEM SHALL record cache reads, cache writes, and reported costs for each dispatch.
- THE SYSTEM SHALL merge only through ci-conductor, after one green run and a verified head SHA.
- THE SYSTEM SHALL invoke ci-conductor directly through its executable, without interface-llm.
- THE SYSTEM SHALL use the provider-neutral claude-mux-lite package, without rswarm.
- THE SYSTEM SHALL provide one fleet-state JSON read and one agent-message command.
- THE SYSTEM SHALL preserve existing planning and dashboard behavior unless this specification changes it.

## Boundaries

The service dependencies are ci-conductor and claude-mux-lite. Every Python import is internal or from the standard library. `git`, `tmux`, and `gh` are command-line programs, not service dependencies. claude-mux-lite communicates with its configured model endpoint over raw HTTP; transports inherit the endpoint configuration and credentials.

A snapshot detects an unleased repository write. The fence restores the path from the base commit, records the refusal, and fails the tool call. Linux uses `bwrap` with a read-only root and writable leaves. Those leaves are the worktree, Git storage, and the scratch directory. A real outside-worktree write refuses. No live shell runs without `bwrap`. The package keeps the MIT notice from its research source in `licenses/RSWARM-MIT.txt`.

The cache rate divides reads by reads, cache writes, and uncached input. A zero denominator gives null. Each task starts a fresh session. A same-task correction continues history. Same-node tasks run consecutively to reuse the provider cache during its TTL.

## Non-goals

This ticket changes no fleet board, slot, registry, or guard. This ticket imports no old board rows. jbi ships no second TUI. The existing TUI calls `fleet` and `send`.

## Slices

1. SPEC, design, static guards, SQLite store, lease lifecycle, and dirty-release refusal.
2. claude-mux-lite dispatch, lease-fenced tools, warm sessions, messages, and cache accounting.
3. Direct CI, merge recording, fleet reads, installation checks, and complete verification.

Each slice gets a draft PR and fresh acceptance. Draft PRs compose into main; no slice bypasses CI.

## Verification

Run `python3 -m unittest discover -s tests -v`. The command exits non-zero on a failure. `tests/test_standalone.py` fails on forbidden external fleet references in the package and lists each import outside the standard library. Guard arms execute leaked-path, path-conflict, and unsafe-release mutants in an archive copy.

## Interfaces

`just-build-it fleet [--run-dir PATH]` prints sessions, tasks, dispatches, leases, CI, merges, costs, and cache rates. `just-build-it send TARGET TEXT [--run-dir PATH]` sends to one session or `--all`. The existing protocol route `message.send` performs the same operation. `docs/STANDALONE-DESIGN.md` defines the schema and lifecycle contracts.