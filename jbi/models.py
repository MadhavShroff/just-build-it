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

from . import cascade, rigor

CATALOG_PATH = Path(__file__).parent / "data" / "models.json"
# The providers a stage can actually dispatch on: agents.command builds an argument
# list only for these. A provider that can name a model but start no agent is never a
# routing target, however healthy its account probe reads.
AGENT_PROVIDERS = ("agy", "claude", "codex", "simulator")
# The order in which enabled providers are tried. The simulator is always last.
# agy is FIRST: founder order 2026-11-17 puts the DAG execution nodes on agy, and
# founder order 2026-11-13 puts no Claude model in any subagent. Claude is kept on
# the list because a run may name it explicitly; it is not the default.
PREFERENCE = ("agy", "codex", "claude", "openai-compatible", "simulator")


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


def _choice(provider: str, model: str, tier: str) -> Choice:
    return Choice(provider, model, tier)


def select(state: dict, role: str, *, task: dict | None = None, attempts: int = 0) -> Choice:
    """The provider and model for one stage.

    FOUNDER ORDERS, 2026-11-16 and 2026-11-17. The scheduler sets no model of its
    own: the cascade decides. A task may then choose between three modes, in
    order of strength:

    prefer   the task names providers in order. The first one the cascade can
             serve is used; the cascade's own rung is used when none of them can.
             This is the "top 2 first, else whatever the cascade says" rule.
    pin      the task names ONE provider and model and runs on nothing else. The
             stage does not start until that model is available. `blocked()`
             answers that question for the supervisor.
    default  no task key: the cascade's rung, exactly as before.

    A `pin` is a hard rule. It is the founder's, never the scheduler's: a task
    that pins a model waits rather than runs somewhere else.
    """
    profile = state["config"]["profile"]
    override = (task or {}).get("override") or {}
    profile = override.get("profile") or profile
    tier = rigor.tier_for(profile, role, attempts)

    rung = cascade.read()
    up = usable(state)

    # A provider the scheduler cannot dispatch on is not "usable" for this purpose,
    # whatever the account probe says. The openai-compatible rung names a model that
    # the local proxy serves; jbi has no agent command for it, so a run routed there
    # dies on its first stage ("provider 'openai-compatible' has no agent command",
    # 7 of 7 tasks, 2026-11-17). Filter to providers that agents.command can start.
    up = [name for name in up if name in AGENT_PROVIDERS]

    # A PIN runs on one model only, and waits for it.
    pinned = override.get("pin")
    if isinstance(pinned, dict) and pinned.get("provider") and pinned.get("model"):
        return _choice(str(pinned["provider"]), str(pinned["model"]), tier)

    # A PREFERENCE list is tried in order, then the cascade.
    prefer = override.get("prefer")
    if isinstance(prefer, list) and prefer:
        for want in prefer:
            if not isinstance(want, dict):
                continue
            provider = want.get("provider")
            if provider and provider in up:
                model = want.get("model") or catalog()["providers"][provider]["models"][tier]
                return _choice(str(provider), str(model), tier)
        # None of the preferred providers is usable. Fall through to the cascade,
        # which may name one of them and be rejected by the account probe later.

    if rung is not None and rung.provider in AGENT_PROVIDERS:
        # A stale rung is still the cascade's last word. Using it beats inventing
        # one, and the staleness is visible in the run record.
        model = rung.model or catalog()["providers"][rung.provider]["models"][tier]
        return _choice(rung.provider, model, tier)

    provider = up[0] if up else "simulator"
    return _choice(provider, catalog()["providers"][provider]["models"][tier], tier)


def blocked(state: dict, task: dict | None = None) -> str | None:
    """Why a task must wait, or None when it may start.

    Only a hard PIN blocks. A pin runs on one model and nothing else, so a stage
    waits for it rather than running somewhere the founder did not choose.
    """
    override = (task or {}).get("override") or {}
    pinned = override.get("pin")
    if not (isinstance(pinned, dict) and pinned.get("provider")):
        return None
    provider = str(pinned["provider"])
    if provider in usable(state):
        return None
    return "pinned to %s / %s, which is not available" % (provider, pinned.get("model") or "?")
