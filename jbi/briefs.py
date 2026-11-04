"""Brief files that start each agent, and the receipt files that agents write back.

A brief has at most 100 lines: goal, owned paths, acceptance, rules, receipt path. A
receipt's first line is `BRIEF <task> | <sha> | PASS/FAIL`. The remaining lines are
the findings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_LINES = 100
RECEIPT_HEAD = re.compile(r"^BRIEF\s+(\S+)\s*\|\s*(\S+)\s*\|\s*(PASS|FAIL)\s*$")

ROLE_GOALS = {
    "implement": "Write the code for this task inside the owned paths. Keep the change small and clear.",
    "verify": "Check the result against the goal and acceptance from outside. Do not edit any file.",
    "review": "Review the diff against main for correctness, quality and readability. Do not edit files.",
    "security": "Check the diff for injection, secrets, authentication flaws and unsafe patterns. "
                "Do not edit files.",
    "bootstrap": "Set up the repository only. Write no feature code.",
}

RULES = (
    "Work only inside the current directory, the task worktree.",
    "Edit only the owned paths. Do not commit; the runtime commits for you.",
    "Never write a secret, a token or a credential into any file.",
    "Write the receipt file last. Its first line decides the result.",
)


@dataclass(frozen=True)
class Receipt:
    task: str
    sha: str
    passed: bool
    findings: list[str] = field(default_factory=list)


def brief_path(run_dir: Path, task: str, stage: str, attempt: int) -> Path:
    return run_dir / "briefs" / f"{task}-{stage}-{attempt}.md"


def receipt_path(run_dir: Path, task: str, stage: str, attempt: int) -> Path:
    return run_dir / "receipts" / f"{task}-{stage}-{attempt}.txt"


def render(*, task: dict, role: str, stage: str, attempt: int, receipt: Path,
           findings: list[str] | None = None, goal: str | None = None) -> str:
    """The brief text for one stage of one task, clipped to MAX_LINES."""
    lines = [f"# Brief {task['id']} — {stage} (attempt {attempt + 1})", "",
             "## Goal", goal or task.get("goal", ""), "", ROLE_GOALS.get(role, ""), ""]
    if task.get("title"):
        lines[2:2] = [f"Task: {task['title']}", ""]
    lines += ["## Owns", *(f"- {p}" for p in task.get("owns") or ["(no declared paths)"]), ""]
    lines += ["## Acceptance", f"`{task.get('acceptance') or 'none'}` must exit 0.", ""]
    if findings:
        lines += ["## Findings from the last attempt", *(f"- {f}" for f in findings[:20]), ""]
    lines += ["## Rules", *(f"- {r}" for r in RULES), ""]
    lines += ["## Receipt", f"Write {receipt}.",
              f"First line: `BRIEF {task['id']} | <commit sha or uncommitted> | PASS` or `... | FAIL`.",
              "Then one finding per line."]
    text_lines = [line for block in lines for line in str(block).splitlines() or [""]]
    return "\n".join(text_lines[:MAX_LINES]) + "\n"


def write(run_dir: Path, *, task: dict, role: str, stage: str, attempt: int,
          findings: list[str] | None = None, goal: str | None = None) -> tuple[Path, Path]:
    """Write the brief for one stage. Returns (brief path, receipt path)."""
    path = brief_path(run_dir, task["id"], stage, attempt)
    receipt = receipt_path(run_dir, task["id"], stage, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.unlink(missing_ok=True)
    path.write_text(render(task=task, role=role, stage=stage, attempt=attempt, receipt=receipt,
                           findings=findings, goal=goal))
    return path, receipt


def parse_receipt(path: Path) -> Receipt | None:
    """Parse a receipt file. Returns None when the file is absent or the first line is malformed."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    match = RECEIPT_HEAD.match(lines[0].strip()) if lines else None
    if not match:
        return None
    findings = [line.strip() for line in lines[1:] if line.strip()]
    return Receipt(match.group(1), match.group(2), match.group(3) == "PASS", findings)
