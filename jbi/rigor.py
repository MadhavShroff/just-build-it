"""Rigor levels, the stage pipeline for each level, and model profiles.

A rigor level decides which stages a task passes before it merges. A model profile
decides which model tier each role uses. A task can override both.
"""

from __future__ import annotations

from dataclasses import dataclass

# Roles that run an agent session. "acceptance" and "merge" run without a model.
AGENT_ROLES = ("implement", "verify", "review", "security")
TIERS = ("XL", "L", "S", "XS")


@dataclass(frozen=True)
class Stage:
    name: str           # unique within a pipeline, for example "review-2"
    role: str           # implement | acceptance | verify | review | security | merge
    label: str


def _stages(*spec: tuple[str, str, str]) -> tuple[Stage, ...]:
    return tuple(Stage(*s) for s in spec)


# FOUNDER RULE 2026-11-16: every review step runs FOUR reviews in parallel.
# One on the model the account preference names first, and three independent
# DeepSeek agents. The three are not coordinated with each other or with the
# first. DeepSeek credits are near-unlimited, so the extra reads cost nothing
# that matters. This is not a per-task choice and not a rigor-level option.
REVIEW_FANOUT = 4
REVIEW_DEEPSEEK_COUNT = 3
REVIEW_CASCADE_STAGE = "review-cascade"
REVIEW_DEEPSEEK_STAGES = ("review-ds-1", "review-ds-2", "review-ds-3")


def review_stages() -> tuple[Stage, ...]:
    """The four review stages every review step runs, in parallel."""
    return (Stage(REVIEW_CASCADE_STAGE, "review", "Review (cascade model)"),
            *(Stage(name, "review", f"Review (DeepSeek {i})")
              for i, name in enumerate(REVIEW_DEEPSEEK_STAGES, start=1)))


TEST_AUDIT_STAGE = "test-audit"
METHODOLOGY_AUDIT_STAGE = "methodology-audit"
DRY_RUN_STAGE = "dry-run"
PRE_MERGE_AUDIT_STAGES: tuple[Stage, ...] = (
    Stage(TEST_AUDIT_STAGE, "review", "Test audit"),
    Stage(METHODOLOGY_AUDIT_STAGE, "review", "Methodology audit"),
    Stage(DRY_RUN_STAGE, "review", "Dry run"),
)


PIPELINES: dict[str, tuple[Stage, ...]] = {
    "vibe": _stages(("implement", "implement", "Implement"), ("merge", "merge", "Merge")),
    "wave-epic": _stages(("implement", "implement", "Implement"), ("acceptance", "acceptance", "Acceptance"),
                         ("merge", "merge", "Epic land")),
    "fast": _stages(("implement", "implement", "Implement"), ("acceptance", "acceptance", "Acceptance"),
                    ("review-1", "review", "Review (diff)"), ("merge", "merge", "Train merge")),
    "medium": _stages(("implement", "implement", "Implement"), ("acceptance", "acceptance", "Acceptance"),
                      *((s.name, s.role, s.label) for s in review_stages()),
                      ("merge", "merge", "Train merge")),
    "high": _stages(("implement", "implement", "Implement"), ("acceptance", "acceptance", "Acceptance"),
                    ("verify", "verify", "Verify"),
                    *((s.name, s.role, s.label) for s in review_stages()),
                    ("security", "security", "Security scan"),
                    *((s.name, s.role, s.label) for s in PRE_MERGE_AUDIT_STAGES),
                    ("merge", "merge", "Train merge")),
    "xhigh": _stages(("implement", "implement", "Implement"), ("acceptance", "acceptance", "Acceptance"),
                     ("verify", "verify", "Verify"),
                     *((s.name, s.role, s.label) for s in review_stages()),
                     ("security", "security", "Security audit"),
                     *((s.name, s.role, s.label) for s in PRE_MERGE_AUDIT_STAGES),
                     ("merge", "merge", "Train merge")),
}

LEVELS: dict[str, dict] = {
    "vibe": {"label": "Vibe coder", "summary": "Builds the features. No tests, reviews or scans.",
             "lane_checks": ()},
    "wave-epic": {"label": "Wave epic",
                  "summary": "Acceptance + clean squash + no-cache tsc onto epic/wave-N; combined review and CI at wave end.",
                  "lane_checks": ("build",)},
    "fast": {"label": "Fast", "summary": "Acceptance command, one read-only diff review, train merge.",
             "lane_checks": ("build", "test")},
    "medium": {"label": "Medium", "summary": "Acceptance command, one review, train merge.",
               "lane_checks": ("build", "test")},
    "high": {"label": "High", "summary": "Adds an independent verifier and a security scan.",
             "lane_checks": ("build", "lint", "test")},
    "xhigh": {"label": "Extra high", "summary": "Two reviews, a security audit and the full lane check set.",
              "lane_checks": ("build", "lint", "test", "format_check")},
}

# Role-based reasoning effort defaults (role effort table).
EFFORT_BY_ROLE: dict[str, str] = {
    "implement": "medium",
    "verify": "high",
    "review": "high",
    "security": "high",
    "plan": "xhigh",
    "bootstrap": "medium",
}

# Task-effort-scaled timeout and turn ceilings (effort timeout table).
TASK_TIMEOUT_BY_EFFORT: dict[str, dict[str, float | int]] = {
    "XS": {"agent_timeout_s": 600.0, "idle_timeout_s": 240.0, "acceptance_timeout_s": 300.0, "max_turns": 36},
    "S":  {"agent_timeout_s": 900.0, "idle_timeout_s": 300.0, "acceptance_timeout_s": 600.0, "max_turns": 60},
    "M":  {"agent_timeout_s": 1800.0, "idle_timeout_s": 420.0, "acceptance_timeout_s": 900.0, "max_turns": 100},
    "L":  {"agent_timeout_s": 2700.0, "idle_timeout_s": 600.0, "acceptance_timeout_s": 1500.0, "max_turns": 150},
    "XL": {"agent_timeout_s": 3600.0, "idle_timeout_s": 600.0, "acceptance_timeout_s": 1800.0, "max_turns": 200},
}

PROFILES: dict[str, dict] = {
    "max": {"label": "Largest everywhere",
            "tiers": {"implement": "XL", "verify": "XL", "review": "XL", "security": "XL", "plan": "XL"}},
    "balanced": {"label": "Large verifies, small builds",
                 "tiers": {"implement": "S", "verify": "L", "review": "L", "security": "L", "plan": "L"}},
    "economy": {"label": "Small everywhere",
                "tiers": {"implement": "S", "verify": "S", "review": "S", "security": "S", "plan": "S"}},
    "micro": {"label": "Very small everywhere",
              "tiers": {"implement": "XS", "verify": "XS", "review": "XS", "security": "XS", "plan": "XS"}},
}

ESCALATE_AFTER = 2      # failed attempts before the implementer moves up one tier
DECISION_AFTER = 5      # consecutive non-advancing failed attempts before asking the user
MAX_CUMULATIVE_ATTEMPTS = 25   # hard circuit breaker across all pipeline stages


def pipeline(level: str) -> tuple[Stage, ...]:
    return PIPELINES[level]


def stage_index(level: str, name: str) -> int:
    """Return the 0-based index of `name` in `PIPELINES[level]`, or -1 when absent."""
    stages = PIPELINES.get(level) or ()
    for idx, item in enumerate(stages):
        if item.name == name:
            return idx
    return -1


def next_stage(level: str, current: str | None) -> Stage | None:
    stages = PIPELINES[level]
    if current is None:
        return stages[0]
    names = [s.name for s in stages]
    if current not in names:
        return stages[0]
    index = names.index(current) + 1
    return stages[index] if index < len(stages) else None


def stage(level: str, name: str) -> Stage:
    return next(s for s in PIPELINES[level] if s.name == name)


def norm_complexity(value: object) -> str:
    """The task's complexity as "S", "M", or "L". Anything else reads as unset."""
    text = str(value or "").strip().upper()
    return text if text in ("S", "M", "L") else ""


def tier_for(profile: str, role: str, attempts: int = 0, complexity: object = None) -> str:
    """The model tier for a role. Repeated failures move the implementer up one tier per step.

    An S-effort build is small work: its implementer runs the S tier of its
    profile's ladder no matter where the profile starts it (audit 4.2), and the
    escalation rule still applies above that floor."""
    tier = PROFILES[profile]["tiers"].get(role, "S")
    if role == "implement" and norm_complexity(complexity) == "S":
        tier = "S"
    if role == "implement" and attempts >= ESCALATE_AFTER:
        steps = (attempts - ESCALATE_AFTER) // ESCALATE_AFTER + 1
        tier = TIERS[max(0, TIERS.index(tier) - steps)]
    return tier


def effective(task: dict, config: dict) -> tuple[str, str]:
    """The (rigor, profile) pair that applies to one task after its overrides.

    An S-effort task with no explicit rigor runs the fast pipeline: small work
    does not need the full ceremony (audit 4.2)."""
    override = task.get("override") or {}
    rigor_name = override.get("rigor") or config["rigor"]
    if norm_complexity(task.get("complexity")) == "S" and not override.get("rigor"):
        rigor_name = "fast"
    return rigor_name, override.get("profile") or config["profile"]


def effort_for(role: str, task: dict | None = None, attempts: int = 0) -> str:
    """Return the reasoning effort for `role`, escalating `implement` after repeated failures."""
    override = (task or {}).get("override") or {}
    if isinstance(override.get("effort"), str) and override["effort"].strip():
        return override["effort"].strip()
    base = EFFORT_BY_ROLE.get(role, "medium")
    if role == "implement" and attempts >= ESCALATE_AFTER and base in ("low", "medium"):
        return "high"
    return base


def task_timeouts(task: dict | None = None) -> dict[str, float | int]:
    """Return wall-clock, idle-output, acceptance timeout, and turn ceiling scaled by task effort."""
    effort = str((task or {}).get("effort") or "M").upper()
    return dict(TASK_TIMEOUT_BY_EFFORT.get(effort, TASK_TIMEOUT_BY_EFFORT["M"]))


def describe() -> dict:
    """Rigor and profile data for the dashboard."""
    return {
        "levels": {k: {**v, "lane_checks": list(v["lane_checks"]),
                       "stages": [s.label for s in PIPELINES[k]]} for k, v in LEVELS.items()},
        "profiles": PROFILES,
    }
