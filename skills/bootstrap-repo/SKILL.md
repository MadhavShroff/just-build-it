---
name: bootstrap-repo
description: Set up a repository so feature work can start. Write no feature code. Record the commands in .jbi/commands.json.
---

# Bootstrap a repository

## Goal
Leave a repository where build, lint, format and test all run clean. Add no feature code.

## Steps
1. For a new project, create the folder and run `git init`. For an existing one, keep the history.
2. Name the app with a short lower-case name.
3. Install the toolchain the stack needs. Pin versions in a manifest or lock file.
4. Add a linter, a formatter and a test runner.
5. Add one tiny source file and one passing test, so each command has input.
6. Add a `.gitignore` and a short README with the commands.
7. Write `.jbi/commands.json`:

```json
{"build": "...", "test": "...", "lint": "...", "format": "...", "format_check": "..."}
```

Each value is one shell command that runs from the repository root. `format_check` exits non-zero when a file needs formatting.

8. Run `build` and `lint` in a fresh shell. Fix every error.
9. Commit with the message `Bootstrap the repository`.

## Rules
- No routes, screens, models or business logic.
- No secret in any file.
- Prefer tools that run with no paid account.
