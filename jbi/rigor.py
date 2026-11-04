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


PIPELINES: dict[str, tuple[Stage, ...]] = {
    "vibe": _stages(("implement", "implement", "Implement"), ("merge", "merge", "Merge")),
    "medium": _stages(("implement", "implement", "Implement"), ("acceptance", "acceptance", "Acceptance"),
                      ("review", "review", "Review"), ("merge", "merge", "Train merge")),
    "high": _stages(("implement", "implement", "Implement"), ("acceptance", "acceptance", "Acceptance"),
                    ("verify", "verify", "Verify"), ("review", "review", "Review"),
                    ("security", "security", "Security scan"), ("merge", "merge", "Train merge")),
    "xhigh": _stages(("implement", "implement", "Implement"), ("acceptance", "acceptance", "Acceptance"),
                     ("verify", "verify", "Verify"), ("review-1", "review", "Review 1"),
                     ("review-2", "review", "Review 2"), ("security", "security", "Security audit"),
                     ("merge", "merge", "Train merge")),
}

LEVELS: dict[str, dict] = {
    "vibe": {"label": "Vibe coder", "summary": "Builds the features. No tests, reviews or scans.",
             "lane_checks": ()},
    "medium": {"label": "Medium", "summary": "Acceptance command, one review, train merge.",
               "lane_checks": ("build", "test")},
    "high": {"label": "High", "summary": "Adds an independent verifier and a security scan.",
             "lane_checks": ("build", "lint", "test")},
    "xhigh": {"label": "Extra high", "summary": "Two reviews, a security audit and the full lane check set.",
              "lane_checks": ("build", "lint", "test", "format_check")},
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
DECISION_AFTER = 4      # failed attempts before the task asks the user


def pipeline(level: str) -> tuple[Stage, ...]:
    return PIPELINES[level]


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


def tier_for(profile: str, role: str, attempts: int = 0) -> str:
    """The model tier for a role. Repeated failures move the implementer up one tier per step."""
    tier = PROFILES[profile]["tiers"].get(role, "S")
    if role == "implement" and attempts >= ESCALATE_AFTER:
        steps = (attempts - ESCALATE_AFTER) // ESCALATE_AFTER + 1
        tier = TIERS[max(0, TIERS.index(tier) - steps)]
    return tier


def effective(task: dict, config: dict) -> tuple[str, str]:
    """The (rigor, profile) pair that applies to one task after its overrides."""
    override = task.get("override") or {}
    return override.get("rigor") or config["rigor"], override.get("profile") or config["profile"]


def describe() -> dict:
    """Rigor and profile data for the dashboard."""
    return {
        "levels": {k: {**v, "lane_checks": list(v["lane_checks"]),
                       "stages": [s.label for s in PIPELINES[k]]} for k, v in LEVELS.items()},
        "profiles": PROFILES,
    }
