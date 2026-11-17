"""Directive files that start each agent, and the receipt files that agents write back.

One directive is one step's system prompt (REQUIREMENT 3.2 in
`~/.tangram-dev/evidence/jbi-run/REQUIREMENT-executor.md`). It is built in two
blocks: the COMMON block, declared once here and true of every step, and the STEP
block, which holds only what is true of that step alone (3.3 and 3.5). A number is
stable: an edit adds a number, it never renumbers one another directive cites (3.4).
A directive that repeats a common instruction is a defect, not a courtesy (3.6).

A directive has at most 100 lines: goal, owned paths, acceptance, rules, receipt
path. A receipt's first line is `BRIEF <task> | <sha> | PASS/FAIL`. The remaining
lines are the findings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_LINES = 100
RECEIPT_HEAD = re.compile(r"^BRIEF\s+(\S+)\s*\|\s*(\S+)\s*\|\s*(PASS|FAIL)\s*$")

# REQUIREMENT 3.3: a shared instruction is declared ONCE, at the top level, and
# carries a number. Numbers 1 to 4 are frozen; a new common instruction takes 5.
COMMON: tuple[tuple[int, str], ...] = (
    (1, "Work only inside the current directory, the task worktree."),
    (2, "Edit only the owned paths. Do not commit; the runtime commits for you."),
    (3, "Never write a secret, a token or a credential into any file."),
    (4, "Write the receipt file last. Its first line decides the result."),
)

# REQUIREMENT 3.5: one directive for the step alone. A stage directive wins over the
# role directive. Numbers continue the COMMON sequence and are never reused.
STAGE_DIRECTIVES: dict[str, tuple[int, str]] = {
    "test-audit": (
        5,
        "Review the tests the app produced and confirm they actually prove the behaviour, "
        "not just that they pass. Do not edit files."
    ),
    "methodology-audit": (
        6,
        "Check the built app against our way of doing and landing code: branch + draft PR, "
        "the work contract headings, tg work:ready, the train, squash merge, no attribution lines, "
        "no --amend, atomic commits. Do not edit files."
    ),
    "dry-run": (
        7,
        "Run an in-weight rehearsal of the app itself: take the built app and walk it through "
        "the real landing path without merging, and report whether it would work in practice and "
        "whether it maps to our way. Do not edit files."
    ),
}

ROLE_DIRECTIVES: dict[str, tuple[int, str]] = {
    "implement": (8, "Write the code for this task inside the owned paths. "
                     "Keep the change small and clear."),
    "verify": (9, "Check the result against the goal and acceptance from outside. "
                  "Do not edit any file."),
    "review": (10, "Review the diff against main for correctness, quality and readability. "
                   "Do not edit files."),
    "security": (11, "Check the diff for injection, secrets, authentication flaws and unsafe "
                     "patterns. Do not edit files."),
    "bootstrap": (12, "Set up the repository only. Write no feature code."),
}


def step_directive(role: str, stage: str) -> tuple[int, str] | None:
    """The one instruction that is true of this step alone, or None."""
    return STAGE_DIRECTIVES.get(stage) or ROLE_DIRECTIVES.get(role)


def common_lines() -> list[str]:
    """The COMMON block, rendered once. `render` copies this text and never edits it."""
    return [f"{number}. {text}" for number, text in COMMON]


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


def _flatten(blocks: list[str]) -> list[str]:
    return [line for block in blocks for line in (str(block).splitlines() or [""])]


def render(*, task: dict, role: str, stage: str, attempt: int, receipt: Path,
           findings: list[str] | None = None, goal: str | None = None) -> str:
    """The directive text for one step of one task, clipped to MAX_LINES.

    REQUIREMENT 4.3: the common block is read once and the step block once, and the
    two are joined. Nothing is copied into the step block.
    """
    step = step_directive(role, stage)
    header_blocks = [f"# Brief {task['id']} — {stage} (attempt {attempt + 1})", ""]
    if task.get("title"):
        header_blocks += [f"Task: {task['title']}", ""]
    header_blocks += ["## Goal", goal or task.get("goal", ""), ""]
    if step:
        header_blocks += [f"Step {step[0]}. {step[1]}", ""]

    middle_blocks: list[str] = []
    if task.get("path") or task.get("spec"):
        refs = [p for p in (task.get("path"), task.get("spec")) if p]
        middle_blocks += ["## Source Docs", *(f"- {r}" for r in refs), ""]
    if task.get("spec_details"):
        details = task["spec_details"]
        detail_lines = details if isinstance(details, list) else [str(details)]
        middle_blocks += ["## Specification Details", *(f"- {d}" for d in detail_lines), ""]
    if task.get("contracts"):
        middle_blocks += ["## Contracts & Interfaces", *(f"- {c}" for c in task["contracts"]), ""]
    if task.get("invariants"):
        middle_blocks += ["## Invariants", *(f"- {inv}" for inv in task["invariants"]), ""]
    if task.get("test_cases"):
        middle_blocks += ["## Required Test Cases", *(f"- {tc}" for tc in task["test_cases"]), ""]
    if task.get("done_criteria"):
        middle_blocks += ["## Done Criteria", *(f"- {dc}" for dc in task["done_criteria"]), ""]
    if findings:
        middle_blocks += ["## Findings from the last attempt", *(f"- {f}" for f in findings[:20]), ""]

    owns_items = list(task.get("owns") or ["(no declared paths)"])
    tail_blocks = [
        "## Acceptance", f"`{task.get('acceptance') or 'none'}` must exit 0.", "",
        "## Rules (common, declared once)", *common_lines(), "",
        "## Receipt", f"Write {receipt}.",
        f"First line: `BRIEF {task['id']} | <commit sha or uncommitted> | PASS` or `... | FAIL`.",
        "Then one finding per line.",
    ]
    header = _flatten(header_blocks)
    middle = _flatten(middle_blocks)
    tail = _flatten(tail_blocks)
    owns = _flatten(["## Owns", *(f"- {p}" for p in owns_items), ""])
    footer = [*owns, *tail]

    if len(header) + len(middle) + len(footer) <= MAX_LINES:
        return "\n".join([*header, *middle, *footer]) + "\n"

    if len(header) + len(footer) > MAX_LINES and len(owns_items) > 1:
        max_owns_lines = max(3, MAX_LINES - len(header) - len(tail))
        keep_count = max(1, max_owns_lines - 3)
        if keep_count < len(owns_items):
            rem = len(owns_items) - keep_count
            owns = _flatten(["## Owns", *(f"- {p}" for p in owns_items[:keep_count]), f"- ... ({rem} more)", ""])
            footer = [*owns, *tail]
    if len(header) + len(footer) > MAX_LINES:
        step_tail = _flatten([f"Step {step[0]}. {step[1]}", ""]) if step else []
        h_budget = max(0, MAX_LINES - len(footer))
        if step_tail and h_budget > len(step_tail):
            header = [*header[:h_budget - len(step_tail)], *step_tail]
        else:
            header = header[:h_budget]

    budget = max(0, MAX_LINES - len(header) - len(footer))
    if budget >= 2:
        kept_middle = [*middle[:budget - 2], "... (truncated)", ""]
    elif budget == 1:
        kept_middle = ["... (truncated)"]
    else:
        kept_middle = []
    return "\n".join([*header, *kept_middle, *footer]) + "\n"


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
