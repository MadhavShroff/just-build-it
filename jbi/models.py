"""The model catalog and model selection.

Selection is a pure function of the state document: the enabled accounts, the task's
effective profile, the role and the attempt count. The runtime and the planner both
call `select`, so they always agree on which model a stage uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import rigor

CATALOG_PATH = Path(__file__).parent / "data" / "models.json"
# The order in which enabled providers are tried. The simulator is always last.
PREFERENCE = ("claude", "codex", "agy", "openai-compatible", "simulator")


@dataclass(frozen=True)
class Choice:
    provider: str
    model: str
    tier: str


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


def usable(state: dict) -> list[str]:
    """Enabled providers whose last probe did not fail, in preference order."""
    accounts = state.get("accounts", {})
    names = [p for p in PREFERENCE
             if accounts.get(p, {}).get("enabled") and accounts[p].get("status") not in ("failed", "missing")]
    return names or ["simulator"]


def select(state: dict, role: str, *, task: dict | None = None, attempts: int = 0) -> Choice:
    profile = state["config"]["profile"]
    override = (task or {}).get("override") or {}
    profile = override.get("profile") or profile
    tier = rigor.tier_for(profile, role, attempts)
    provider = usable(state)[0]
    model = override.get("model") or catalog()["providers"][provider]["models"][tier]
    return Choice(provider, model, tier)
