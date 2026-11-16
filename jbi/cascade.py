"""Read the model and provider the cascade service selects.

The scheduler sets NO model of its own. Founder order 2026-11-16: "the scheduler
should NOT set models, that is left to the cascade. It can read from the cascade
service to know what model and provider to dispatch work on."

The cascade writes ~/.tangram-dev/cascade-state.json. Its `model` field is the id
the launcher uses, and its `verdict` names the rung. This module maps that to the
provider that can serve it, so a stage dispatch never invents a model.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

STATE = Path(os.environ.get("CASCADE_STATE", "/home/madhavshroff/.tangram-dev/cascade-state.json"))
# The rung a verdict names, and the provider that serves that rung's models.
# A model id may also name the provider outright, which wins.
VERDICT_PROVIDER = (
    (("claude",), "claude"),
    (("glm",), "openai-compatible"),
    (("morph",), "openai-compatible"),
    (("local",), "openai-compatible"),
)
MODEL_MARKERS = (
    ("claude-", "claude"),
    ("anthropic-glm", "openai-compatible"),
    ("anthropic-morph", "openai-compatible"),
    ("gpt-", "codex"),
    ("gemini-", "agy"),
)
MAX_AGE_H = 12


@dataclass(frozen=True)
class Rung:
    verdict: str
    model: str
    provider: str
    age_h: float
    stale: bool
    why: str


def read() -> Rung | None:
    """The cascade's current rung, or None when its file is absent or unreadable."""
    try:
        data = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict") or "")
    model = str(data.get("model") or "")
    # A verdict only names a rung; the model is what a dispatch needs. The history
    # carries the model for a rung that the top-level record omits.
    if not model:
        for row in reversed(data.get("history") or []):
            if isinstance(row, dict) and row.get("model") and row.get("verdict") == verdict:
                model = str(row["model"])
                break
    try:
        age_h = (time.time() - float(data.get("checked_at") or 0)) / 3600.0
    except (TypeError, ValueError):
        age_h = 999.0
    provider = ""
    for marker, name in MODEL_MARKERS:
        if model.startswith(marker):
            provider = name
            break
    if not provider:
        for prefixes, name in VERDICT_PROVIDER:
            if verdict.startswith(prefixes):
                provider = name
                break
    return Rung(verdict=verdict, model=model, provider=provider or "agy",
                age_h=round(age_h, 1), stale=age_h > MAX_AGE_H,
                why=str(data.get("action") or "")[:300])
