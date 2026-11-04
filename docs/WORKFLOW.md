# The just-build-it method

This is the method that `just-build-it` applies to every project. The code enforces each
rule that it can. A rule the code cannot enforce is a rule for the agents, in `skills/`.

## 1. Plan as a DAG, run by dependency

- The planner turns the user's intent into tasks. Each task has one goal, its dependencies,
  the paths it owns, and an acceptance command that exits non-zero on failure.
- The compiler validates the plan and refuses a cycle by name: `a -> b -> c -> a`.
- Waves group tasks for display. They are Kahn levels of the DAG.
- **A task starts the moment its own dependencies are merged. It never waits for its wave.**
- Two running tasks never own the same path. The scheduler holds the second one back.

## 2. One task, one brief, one fresh agent

- Each stage of each task runs in a fresh agent session with a brief of 100 lines or fewer.
- A brief names facts, not wishes: the goal, the files to read, the files to edit, the rules,
  the acceptance command, and the receipt path.
- The agent writes a receipt. The first line is fixed: `BRIEF <task> | <sha> | PASS/FAIL`.
- PASS means that the acceptance command exited 0. A missing tool is a FAIL.
- Only the supervisor commits. Agents edit files in their own worktree.
- A FAIL starts a new agent with the findings. It never resumes the old session.

## 3. Rigor is a choice, and the choice is visible

| Level | What a task passes before it merges |
|---|---|
| Vibe coder | Implement. Nothing else. |
| Medium | Implement, acceptance, one review. |
| High | Implement, acceptance, independent verify, one review, security scan. |
| Extra high | Implement, acceptance, independent verify, two reviews, security audit, full lane checks. |

The user can change the level for the run or for one task, at any time.
A change applies to the next stage that starts.

## 4. Cheap models build, strong models check

- A model profile maps each role to a tier: XL, L, S, or XS.
- The default profile, `balanced`, implements with a small model and verifies with a large one.
- Two failed attempts move the implementer up one tier.
- Four failed attempts open a decision for the user: retry, skip, or pause.

## 5. Verify from outside

- The verifier is a different agent from the implementer. It reads the brief and the diff.
  It runs the acceptance command itself. It does not edit code.
- A claim about the system names the command that proves it, or says "inferred, not verified".
- Nobody edits a test to make it pass.

## 6. Land through a CI lane

- Each lane pins the base commit, squash-merges the task branch onto it in a temporary
  worktree, and runs the checks for the task's rigor level.
- A lane merges only if the base is still the pinned commit and the tested tree equals the
  merged tree. If the base moved, the lane rebuilds and checks again.
- A lane caches each result by its inputs: base, head, and check set. The same inputs never
  run twice.
- The lane count follows the machine: one lane for each four CPU cores, from one to four.

## 7. Build only the latest design

- A new project has no old users and no old data. Build no migration, legacy reader,
  two-format read, or old-data fallback.
- Keep isolation, authentication, export, erase, backup, and restore.
- In an existing repository, the repository's own compatibility rules win.

## 8. The board tells the truth

- Every change is an event with a sequence number. The dashboard renders the events.
- Nothing is marked merged before the lane records the merge commit.
- A value that a provider does not report is "unknown", never zero.
- A question that needs the user becomes a decision on the board, with the evidence beside it.

## 9. Every process belongs to the run

- Agents run in tmux windows on a private tmux server for the run.
- Each agent runs under a shim that watches the CLI process. If the CLI dies, the shim
  kills the agent's process group.
- The user stops the run with one of four modes: quit, stand down, finish then stop, kill.
