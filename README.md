# just-build-it

A CLI with a web dashboard that builds software end to end.

You describe what to build. `just-build-it` plans it as a dependency graph, proposes the
cheapest workable tech stack, sets up the repository, and then runs a fleet of coding
agents in tmux windows. CI lanes verify and merge each task. You watch every step live in
your browser, and you can change the rigor and the models at any time.

```
$ ./just-build-it

just-build-it 0.1.0 · run r-20261104-2015

Open the dashboard. Setup continues in the browser.

  Local     http://127.0.0.1:52811/#k=…
  Tailscale http://100.64.0.7:52811/#k=…

Stop options
  1  Quit now. Agents stop at once; unsaved work can be lost.
  2  Stand down. Agents save work in progress to their branches, then stop.
  3  Finish assigned tasks, then stop. No new task starts.
  4  Kill. End this process and every process it started, immediately.
```

## How a run goes

1. **Describe.** Talk to the planning chat, or paste a task list. Choose how much detail you
   give: everything, answers to a few questions, or none (the planner decides). Name a tech
   stack if you have one. Stop the questions whenever you like.
2. **Stack.** The dashboard shows one line: `Proposed tech stack: …`. Proceed, or ask for
   changes.
3. **Bootstrap.** One agent sets up the repository at `~/<app-name>` (you can change the
   folder): toolchain, linters, formatter, test runner, packages, and a first build. No
   feature code yet.
4. **Configure.** Choose a rigor level: Vibe coder, Medium, High, or Extra high. Choose a
   model profile: largest everywhere, large verifies and small builds, small everywhere, or
   very small everywhere.
5. **Build.** Tasks start as soon as their dependencies merge. The board shows the task
   graph, a live architecture diagram, the agent sessions, the CI lanes, usage, and any
   decision that needs you.

To add features to an existing project: `./just-build-it --repo ~/my-project`.

## Requirements

- Python 3.11 or newer. The tool uses the standard library only.
- git and tmux.
- At least one coding agent CLI that you are signed in to: `claude`, `codex`, or `agy`.
  An OpenAI-compatible local endpoint also works. `--offline` runs a simulator instead.
- Tailscale is optional. When it runs, the CLI also prints a Tailscale link.

## For machines

The browser, the CLI and scripts use one HTTP contract: one state document, one event
stream of JSON Patch operations, and one command endpoint. `GET /api/v1/schema` describes
every command and event. See [docs/PROTOCOL.md](docs/PROTOCOL.md).

```
./just-build-it status                         # the state document as JSON
./just-build-it send run.configure '{"rigor":"high","profile":"balanced"}'
```

## Documents

- [docs/SPEC.md](docs/SPEC.md): the product specification
- [docs/PROTOCOL.md](docs/PROTOCOL.md): the wire contract
- [docs/WORKFLOW.md](docs/WORKFLOW.md): the method that the agents follow
- [skills/](skills/): the skills that each agent session receives

## Tests

```
python3 -m unittest discover -s tests -v
```
