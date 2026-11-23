"""CI declaration checks and a merge job record. Only ci-conductor can land code."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .shim import Groups, run_command
from .agents import child_env

CHECK_TIMEOUT = 1800.0


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    notes: list[str]


def run_checks(checks: tuple[str, ...], tree_dir: Path, *, parent_pid: int | None = None,
               groups: Groups | None = None) -> CheckResult:
    """Run a repository's declared CI commands. Never land a commit."""
    if not checks:
        return CheckResult(True, ["no lane checks for this rigor level"])
    path = tree_dir / ".jbi" / "commands.json"
    if not path.exists():
        return CheckResult(True, [f"{name}: skipped, no command in .jbi/commands.json" for name in checks])
    try:
        commands = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return CheckResult(False, [f".jbi/commands.json is invalid: {error}"])
    if not isinstance(commands, dict):
        return CheckResult(False, [".jbi/commands.json must contain a JSON object"])
    notes = []
    for name in checks:
        line = commands.get(name)
        if not isinstance(line, str) or not line.strip():
            return CheckResult(False, [f"{name} failed (missing command in .jbi/commands.json)"])
        result = run_command(line, cwd=tree_dir, env=child_env(None),
                             timeout=CHECK_TIMEOUT, parent_pid=parent_pid, groups=groups)
        if result.returncode:
            return CheckResult(False, [f"{name} failed ({result.returncode}): {result.output[-500:]}"])
        notes.append(f"{name}: passed")
    return CheckResult(True, notes)


@dataclass(frozen=True)
class MergeJob:
    task: str
    branch: str
    level: str
    worktree: Path
    target_branch: str | None = None
