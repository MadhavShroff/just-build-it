"""The bootstrap brief and its acceptance check.

Bootstrap sets up the repository and writes no feature code. The brief tells one agent
what to install and what to record in `.jbi/commands.json`. `check` proves the result
from outside the agent: it runs `build` and `lint` itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from .agents import child_env
from .shim import Groups, run_command

COMMANDS_FILE = ".jbi/commands.json"
COMMAND_KEYS = ("build", "test", "lint", "format", "format_check")
CHECKED = ("build", "lint")
CHECK_TIMEOUT = 900


def brief(state: dict) -> str:
    """The bootstrap brief, at most 100 lines."""
    target = state["run"]["target"]
    new = state["run"].get("mode", "new") == "new"
    prd = state.get("plan", {}).get("prd", {})
    layers = [f"- {l['layer']}: {l['choice']} ({l['reason']})" for l in state.get("stack", {}).get("layers", [])]
    lines = [
        f"# Brief bootstrap — set up the repository for {prd.get('name') or state['run'].get('name') or 'the project'}",
        f"Repository: {target}",
        "",
        "## Goal",
        "Make the repository ready for feature work. Write NO feature code.",
        "A later agent builds each feature. Your job ends when the toolchain runs clean.",
        "",
        "## Stack (accepted by the user)",
        *(layers or ["- No layers recorded. Choose the simplest toolchain for the project."]),
        "",
        "## Steps",
        ("1. Create the directory if it is missing and run `git init -b main`." if new else
         "1. Keep the existing repository and history. Do not rewrite any tracked file without need."),
        "2. Name the app. Use a short lower-case name in the package manifest.",
        "3. Install the toolchain for the stack: compiler or runtime, package manager, and packages.",
        "4. Add a linter, a formatter and a test runner. Pin versions in the manifest or lock file.",
        "5. Add the smallest source file and one passing test so that build, lint and test have input.",
        "6. Add a .gitignore for the stack. Add a short README with the run, test and lint commands.",
        f"7. Write `{COMMANDS_FILE}` as one JSON object with these keys:",
        "   build, test, lint, format, format_check. Each value is one shell command that runs from the",
        "   repository root. format_check exits non-zero when a file needs formatting.",
        "8. Run build and lint in a fresh shell. Fix every error.",
        "9. Commit the result with the message `Bootstrap the repository`.",
        "",
        "## Rules",
        "- Do not add feature code, routes, screens, models or business logic.",
        "- Do not store a secret, token or key in any file.",
        "- Use only tools that run without a paid account.",
        "",
        "## Acceptance (the system runs this itself)",
        f"`{COMMANDS_FILE}` exists, and its `build` and `lint` commands exit 0.",
        "",
        "## Receipt",
        "Write the receipt file named in the launch message.",
        "First line: `BRIEF bootstrap | <commit sha> | PASS` or `FAIL`.",
        "Then DID (what you set up), PROOF (the commands you ran and their exit codes), LEFT (open problems).",
    ]
    assert len(lines) <= 100
    return "\n".join(lines) + "\n"


def check(repo: Path, *, parent_pid: int | None = None, groups: Groups | None = None) -> tuple[bool, str]:
    """Run the recorded `build` and `lint` commands. A missing command is a FAIL."""
    path = Path(repo) / COMMANDS_FILE
    try:
        commands = json.loads(path.read_text())
    except FileNotFoundError:
        return False, f"{COMMANDS_FILE} does not exist."
    except (json.JSONDecodeError, OSError) as error:
        return False, f"{COMMANDS_FILE} is not readable JSON: {error}"
    if not isinstance(commands, dict):
        return False, f"{COMMANDS_FILE} must hold a JSON object."
    notes = []
    for key in CHECKED:
        command = commands.get(key)
        if not isinstance(command, str) or not command.strip():
            return False, f"{key}: no command recorded in {COMMANDS_FILE}; {key} must exit 0."
        result = run_command(command, cwd=Path(repo), env=child_env(None), timeout=CHECK_TIMEOUT,
                             parent_pid=parent_pid, groups=groups)
        if result.timed_out:
            return False, f"{key} timed out after {CHECK_TIMEOUT} seconds."
        if result.returncode != 0:
            return False, f"{key} exited {result.returncode}: {result.output.strip()[-400:]}"
        notes.append(f"{key}: exit 0")
    return True, "; ".join(notes)
