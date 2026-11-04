# just-build-it — planning specification

Version 1, 2026-11-04. This file is the product contract. Code follows this file.

## 1. Goal

`just-build-it` turns an idea into a working software repository.
The user runs one command. The command opens a local web dashboard.
The dashboard takes the user from an idea to merged, verified code.
A fleet of coding agents does the work in tmux windows.
The user sees every step live in the browser.

The product works for a new project and for new features in an existing repository.
The product is independent. It has no dependency on any other orchestration system.

## 2. Non-goals

- The product does not host code or run a cloud service.
- The product does not bypass provider terms, quotas, or authentication.
- The product does not push to a remote repository unless the user enables push.
- The product does not collect telemetry outside the user's machine.
- The product does not promise that a provider model is available. A probe proves availability.

## 3. Phases

A run moves through fixed phases. The server owns the phase. The browser only displays it.

| Phase | What happens | Exit condition |
|---|---|---|
| `intake` | The user imports a task list or talks with the planning chat. | The user selects **Stop questions and plan**. |
| `stack` | The system shows one proposed tech stack, calibrated to the cheapest workable options. | The user selects **Proceed**, or edits and then proceeds. |
| `bootstrap` | One agent sets up the repository only: init, name, toolchain, linters, packages, first build. No feature work. | The bootstrap acceptance command exits 0. |
| `configure` | The user selects a rigor level and a model profile. | The user selects **Start build**. |
| `execute` | The scheduler runs the task DAG. CI lanes verify and merge each task. | Every task is `merged`, or the user stops the run. |
| `complete` | The run is finished. The dashboard keeps the full record. | — |

An existing-repository run skips repository creation in `bootstrap`. Bootstrap then only verifies the toolchain.
The user can return from `stack` to `intake`. No other phase goes back.

## 4. Intake

### 4.1 Two input paths

1. **Import.** The user pastes or uploads a task list in any form.
   The system converts the list to a rough PRD, then a formal PRD, then tasks and waves.
   Each step keeps links to the source lines.
2. **Chat.** The planning chat asks the user what to build.
   The user picks a detail level: `user` (the user gives details), `guided` (the chat asks), or `delegate` (the chat decides).
   The user can change the detail level at any time.
   The user can name a tech stack in the chat. The stack proposal then keeps that choice.
   The chat never forces a minimum number of questions.
   The user ends questions with **Stop questions and plan**.

### 4.2 Plan output

The planner writes three artifacts. Each has a revision number.

- `rough_prd`: free text that restates the ask.
- `prd`: a structured document: problem, users, features, constraints, assumptions, components.
- `plan`: the executable task DAG. The schema is in section 9.

The planner records every choice it made for the user as an assumption.
The planner also proposes the initial architecture graph (section 11).

## 5. Stack proposal

The stack proposal is one short block:

```
Proposed tech stack: Python 3.12 · SQLite · htmx · Fly.io free tier
[ Proceed ]   [ Make changes ]
```

Rules:

- The proposal is calibrated to the cheapest workable option for each layer: free tiers, local databases, no paid service by default.
- Each layer shows its choice, one reason, and its cost class: `free`, `low`, or `paid`.
- A stack that the user named in intake has priority over the cheapest option.
- **Make changes** opens a text field. The planner revises the proposal and shows it again.
- **Proceed** accepts the proposal and starts `bootstrap`.

## 6. Bootstrap

The bootstrap agent sets up the repository. It writes no feature code.

- The default target is `~/<project-slug>`. The user can edit the target before **Proceed**.
- A new target must be empty or absent. The system refuses a non-empty target for a new project.
- The agent creates the repository, names the app, and installs the compilers, linters, formatters, test runner, and packages for the stack.
- The agent writes `.jbi/commands.json` with the commands `build`, `test`, `lint`, and `format`.
- Bootstrap passes when `build` and `lint` exit 0 in a fresh shell.
- The system commits the bootstrap result as the base commit for the build.
- An existing repository keeps its current branch. A detached HEAD is refused.
- A failed bootstrap offers **Retry bootstrap** and **Back to the stack**.

## 7. Rigor levels

The user picks one level for the run. The user can override the level per task, at any time.

| Level | Stages for each task |
|---|---|
| `vibe` | implement → merge. No tests, no review, no security scan, no validation. |
| `medium` | implement → acceptance → one review → train merge. |
| `high` | implement → acceptance → independent verify → one review → security scan → train merge. |
| `xhigh` | implement → acceptance → independent verify → two reviews → security audit → train merge with the full check set. |

Stage meanings:

- **implement**: one agent writes the code from a brief.
- **acceptance**: the system runs the task's acceptance command. Non-zero means FAIL.
- **verify**: a separate agent checks the result against the brief, from outside. It does not edit code.
- **review**: an agent reviews the diff for correctness, quality, and readability. Findings go back to a new implementer.
- **security scan / audit**: an agent checks the diff for injection, secrets, authentication, and unsafe patterns. The audit also checks dependencies.
- **train merge**: a CI lane tests the combined tree on a pinned base and squash-merges a verified head.

A FAIL in any stage sends the task back to `implement` with the findings. After two FAILs the next attempt uses the next larger model tier. After four FAILs the task opens a decision for the user.

## 8. Model profiles

Each rigor level has a model profile. A profile maps a role to a model tier.

| Profile | implement | verify, review, research | security |
|---|---|---|---|
| `max` | XL | XL | XL |
| `balanced` | S | L | L |
| `economy` | S | S | S |
| `micro` | XS | XS | XS |

The model catalog (`jbi/data/models.json`) maps each tier to a model for each provider.
The user enables providers on the **Accounts** panel. The system picks the first enabled provider that passes its probe.
The user can override the profile or a single model per task, at any time. A change applies to the next stage that starts.

## 9. Task plan schema

```json
{
  "revision": 3,
  "tasks": {
    "core": {"id": "core", "title": "Build the core", "goal": "...", "deps": [],
             "acceptance": "python3 -m unittest", "owns": ["src/core.py"],
             "components": ["api"], "wave": 1, "status": "ready"}
  },
  "order": ["core", "cli"],
  "waves": [["core"], ["cli"]]
}
```

Validation: unique ids that match `^[a-z][a-z0-9-]{0,39}$`, known dependencies, no self-dependency, no cycle.
The compiler computes waves as Kahn topological levels in plan order.
A cycle is refused with its path named, for example `a -> b -> c -> a`.
A wave is a display group only. A task starts when its own dependencies are merged. It never waits for its wave.

Task status values: `blocked`, `ready`, `running`, `verifying`, `queued`, `merging`, `merged`, `failed`, `paused`, `decision`, `skipped`.
A skipped task never counts as merged. Its dependents stay `blocked` and show the reason.

## 10. Execution

### 10.1 Sessions

- Every agent runs in its own tmux window on a private tmux server: `tmux -L jbi-<run-id>`.
- The user can attach with `tmux -L jbi-<run-id> attach`. The dashboard shows this command.
- Every task gets its own git worktree and branch `jbi/<task-id>`.
- A brief file of 100 lines or fewer starts each agent. The agent writes a receipt. The first line is `BRIEF <task> | <sha> | PASS/FAIL`.
- At most `max_sessions` agents run at the same time. The default is 3.
- Two running tasks never own the same path.

### 10.2 CI lanes

- The system sets the lane count from the machine: `clamp(cpu_count // 4, 1, 4)`.
- Each lane is a tmux window. A lane takes one finished task at a time.
- A lane pins the base SHA, squash-merges the task branch onto it in a temporary worktree, and runs the checks for the task's rigor level.
- A lane merges only when the base is still the pinned SHA and the tested tree equals the merged tree.
- If the base moved, the lane rebuilds the combined tree and runs the checks again.
- A lane caches a result by the inputs (base SHA, head SHA, check set). The same inputs never run twice.

### 10.3 Process tree and stop modes

Every process that the run starts is linked to the CLI process.
The agent shim watches the CLI process. When the CLI process dies, the shim kills its agent process group.
The CLI control menu, and the dashboard, offer four stop modes:

| Mode | Effect |
|---|---|
| `quit` | Stop now. Terminate every agent with SIGTERM. Unsaved work can be lost. |
| `standdown` | Stop gracefully. Interrupt every agent, commit its work in progress to its branch, mark the task `paused`, then exit. |
| `finish` | Start no new task. Let running tasks complete and merge. Then exit. |
| `kill` | Kill the CLI, the tmux server, and every child process group with SIGKILL. |

## 11. Architecture view

The board shows a technical diagram: services, clients, databases, queues, and external systems, with directed edges for what talks to what.

- The planner creates the first graph from the PRD at the end of intake.
- The `architecture.sync` hook runs at fixed points: `plan.approved`, `bootstrap.completed`, `task.merged`, `wave.completed`, and on request.
- The hook decides if the graph must change. Most events change only status.
- An agent can propose a change in `.jbi/architecture.patch.json` in its worktree. The hook validates the patch before it applies it.
- Each node and edge has a status: `planned`, `building`, `built`, `failed`. The status comes from the tasks linked to the element.
- The graph supports pan, zoom, search, and selection. Selection shows the linked tasks and evidence.

## 12. Telemetry

Telemetry is local only. It lives in the run database.

- Every state change is an event with a sequence number.
- Tracked values: tokens in and out, cost reported by the provider, stage durations, queue waits, failures, retries, lane runs.
- A value that a provider does not report stays unknown. The system never shows unknown as zero.
- The system stores no prompt text, credential, or header in telemetry.

## 13. Web dashboard

- The CLI starts the server and prints a loopback link.
- The CLI checks for Tailscale. If Tailscale is up, the server also listens on the Tailscale address and the CLI prints that link. The CLI probes each link before it prints it.
- The server never listens on `0.0.0.0`.
- Each link carries a one-time key in the URL fragment. The browser exchanges the key for an HttpOnly session cookie and removes the key from the address bar.
- State-changing requests need the session cookie, `Content-Type: application/json`, and a matching `Origin`.
- The browser loads one snapshot, then follows the event stream. Section 14 defines the wire contract.
- Setup happens in the browser: accounts, intake, stack, bootstrap target, rigor, models, and per-task overrides.
- The dashboard must look excellent: clear hierarchy, calm motion, live beat-by-beat updates, light and dark themes, keyboard access.

## 14. Wire contract

`docs/PROTOCOL.md` is the normative wire contract. Summary:

- `GET /api/v1/state` returns `{protocol, seq, state}`.
- `GET /api/v1/events?after=<seq>` is a Server-Sent Events stream. Each event carries JSON Patch operations (RFC 6902) on the state document.
- `POST /api/v1/commands` takes `{id, type, payload}` and returns `202 {accepted, seq}` or an error object.
- `GET /api/v1/schema` returns the command and event catalog, so a machine client can discover the contract.
- The CLI uses the same HTTP contract as the browser. There is no second channel.

## 15. Secrets

- Provider CLIs keep their own login. The system calls their status commands and never reads their token files.
- An optional API key goes to the operating system keychain: `security` on macOS, `secret-tool` on Linux.
- If no keychain exists, the key lives in process memory for the run only. The dashboard says so.
- No secret goes into the repository, the run database, a log, a brief, or a command line argument.

## 16. Acceptance

- WHEN the user runs `just-build-it` THE SYSTEM SHALL start the dashboard and print each verified link.
- WHEN Tailscale is up THE SYSTEM SHALL print a verified Tailscale link next to the loopback link.
- WHEN the user stops questions THE SYSTEM SHALL produce a PRD, a task DAG, and an architecture graph.
- WHEN the plan has a cycle THE SYSTEM SHALL refuse it and name the cycle.
- WHEN the plan is ready THE SYSTEM SHALL show one cheapest-first stack proposal with Proceed and Make changes.
- WHEN the user proceeds THE SYSTEM SHALL bootstrap the repository at the chosen target without feature code.
- WHEN a task's dependencies are merged THE SYSTEM SHALL start that task without waiting for its wave.
- WHEN rigor is `vibe` THE SYSTEM SHALL merge without tests, reviews, or scans.
- WHEN a lane's base moves THE SYSTEM SHALL rebuild and recheck before it merges.
- WHEN the user selects a stop mode THE SYSTEM SHALL apply exactly that mode to every child process.
- WHEN the CLI process dies THE SYSTEM SHALL terminate every agent that the run started.
- WHEN a provider does not report a value THE SYSTEM SHALL show the value as unknown.
- WHEN the browser reconnects THE SYSTEM SHALL resume after the last sequence number it applied.

## 17. Repository layout

```
just-build-it            CLI entry point
jbi/                     Python package, standard library only
  protocol.py            wire contract: commands, events, validation
  jsonpatch.py           RFC 6902 subset used by server and tests
  store.py               SQLite event log and state document
  server.py              HTTP, SSE, sessions, static files
  netinfo.py             loopback and Tailscale links, probes
  cli.py                 argument parsing and the control menu
  engine.py              command handlers and the phase machine
  waves.py               plan validation and Kahn waves
  planner.py             import, chat, PRD, plan generation
  stack.py               cheapest-first stack proposal
  bootstrap.py           bootstrap brief and acceptance
  architecture.py        graph model and the architecture.sync hook
  rigor.py               rigor levels, stages, model profiles
  providers.py           provider adapters and probes
  supervisor.py          tmux sessions, process tree, stop modes
  shim.py                per-agent wrapper inside each tmux window
  lanes.py               CI lanes and the merge train
  worktrees.py           git worktree helpers
  briefs.py              brief and receipt files
  secrets.py             keychain access
  simulator.py           offline agent for demos and tests
  data/models.json       model catalog
web/                     dashboard: HTML, CSS, ES modules, no build step
skills/                  agent skills installed into each worktree
tests/                   unittest suites
docs/                    SPEC, PROTOCOL, WORKFLOW
```
