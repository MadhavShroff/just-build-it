"""The model catalog and model selection.

Selection is a pure function of the state document: the enabled accounts, the task's
effective profile, the role and the attempt count. The runtime and the planner both
call `select`, so they always agree on which model a stage uses.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from . import rigor

CATALOG_PATH = Path(__file__).parent / "data" / "models.json"
# The providers a stage can actually dispatch on: agents.command builds an argument
# list only for these. A provider that can name a model but start no agent is never a
# routing target, however healthy its account probe reads.
AGENT_PROVIDERS = ("agy", "claude", "codex", "openai-compatible")
# The order in which enabled providers are tried. The simulator is always last.
# agy is FIRST: founder order 2026-11-17 puts the DAG execution nodes on agy, and
# founder order 2026-11-13 puts no Claude model in any subagent. Claude is kept on
# the list because a run may name it explicitly; it is not the default.
PREFERENCE = ("agy", "codex", "claude", "openai-compatible", "simulator")
# The founder cascade, amended 2026-11-24: opus 5.5 first, then Gemini Neon, falling
# back to Carbon. `--cascade founder` installs it as the run's provider order. No
# morph model is in it: morph answers 401, and nothing in jbi may depend on it.
FOUNDER_CASCADE = (
    {"provider": "claude", "model": "claude-opus-5-5"},
    {"provider": "agy", "model": "gemini-4-neon-high"},
    {"provider": "agy", "model": "gemini-4-carbon-high"},
)


@dataclass(frozen=True)
class Choice:
    provider: str
    model: str
    tier: str


class ProviderCooldowns:
    """Thread-safe per-provider rate-limit cooldown tracker (provider retry helper)."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._until: dict[str, float] = {}

    def mark(self, provider: str, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._until[provider] = max(self._until.get(provider, 0.0), self._clock() + seconds)

    def cooling(self, provider: str) -> bool:
        with self._lock:
            until = self._until.get(provider, 0.0)
            if until <= self._clock():
                self._until.pop(provider, None)
                return False
            return True

    def remaining(self, provider: str) -> float:
        with self._lock:
            return max(0.0, self._until.get(provider, 0.0) - self._clock())

    def lanes(self) -> dict[str, float]:
        """Every lane still cooling, with its remaining seconds. Never raises."""
        with self._lock:
            now = self._clock()
            return {lane: round(max(0.0, until - now), 1)
                    for lane, until in self._until.items() if until > now}


class ProviderPool:
    """Weighted multi-provider round-robin pool (provider retry helper).

    Entries are `(provider, model_or_none)` tuples. Repeating an entry creates a weighted
    ratio (for example, 2:1 agy:claude). Rate-limited providers are skipped while cooling.
    """

    def __init__(self, entries: Iterable[tuple[str, str | None]] = ()) -> None:
        self._entries = [(str(p), str(m) if m else None) for p, m in entries if p]
        self._lock = threading.Lock()
        self._cursor = 0

    def __bool__(self) -> bool:
        return bool(self._entries)

    def next(self, usable_providers: Iterable[str], tier: str, *,
             cooldowns: ProviderCooldowns | None = None) -> Choice | None:
        allowed = set(usable_providers)
        if not self._entries or not allowed:
            return None
        with self._lock:
            n = len(self._entries)
            for offset in range(n):
                idx = (self._cursor + offset) % n
                provider, model = self._entries[idx]
                if provider not in allowed:
                    continue
                if cooldowns is not None and cooldowns.cooling(provider):
                    continue
                self._cursor = (idx + 1) % n
                resolved = model or catalog()["providers"][provider]["models"][tier]
                return _choice(provider, resolved, tier)
            # Every pooled provider is cooling: fall back to the least-cooled usable one.
            usable_entries = [(i, p, m) for i, (p, m) in enumerate(self._entries) if p in allowed]
            if not usable_entries:
                return None
            idx, provider, model = min(
                usable_entries,
                key=lambda item: cooldowns.remaining(item[1]) if cooldowns else 0.0,
            )
            self._cursor = (idx + 1) % n
            resolved = model or catalog()["providers"][provider]["models"][tier]
            return _choice(provider, resolved, tier)


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


def reviewer_overrides() -> dict[str, tuple[str, str]]:
    """The catalog's `reviewers` map: stage name -> (provider, model).

    The parallel review step names one model per reviewer stage here, so four
    reviewers read four ways instead of four identical ways. A stage the map does
    not name runs the routing default and may read as a duplicate."""
    raw = catalog().get("reviewers") or {}
    out: dict[str, tuple[str, str]] = {}
    for name, want in raw.items():
        if isinstance(want, dict) and want.get("provider") and want.get("model"):
            out[str(name)] = (str(want["provider"]).strip().lower(), str(want["model"]))
    return out


def usable(state: dict) -> list[str]:
    """Enabled providers whose last probe did not fail, in preference order."""
    accounts = state.get("accounts", {})
    names = [p for p in PREFERENCE
             if accounts.get(p, {}).get("enabled") and accounts[p].get("status") not in ("failed", "missing")]
    return names or ["simulator"]


def _choice(provider: str, model: str, tier: str) -> Choice:
    return Choice(provider, model, tier)


def select(state: dict, role: str, *, task: dict | None = None, attempts: int = 0,
           pool: ProviderPool | None = None, cooldowns: ProviderCooldowns | None = None) -> Choice:
    """The provider and model for one stage.

    The scheduler picks a model in this order: the task pin, the task preference
    list, the run cascade, the provider pool, then the enabled account preference.

    pin      the task names ONE provider and model and runs on nothing else. The
             stage does not start until that model is available. `blocked()`
             answers that question for the supervisor.
    prefer   the task names providers in order. The first usable one wins.
    cascade  the run names providers in order (`--cascade founder`). The first
             usable entry wins.
    pool     the weighted provider pool, when one is supplied.
    default  the first enabled provider in PREFERENCE order.

    A `pin` is a hard rule. A task that pins a model waits rather than runs
    somewhere else.
    """
    profile = state["config"]["profile"]
    override = (task or {}).get("override") or {}
    profile = override.get("profile") or profile
    tier = rigor.tier_for(profile, role, attempts, complexity=(task or {}).get("complexity"))

    up = usable(state)

    # Only providers supported by the neutral subprocess may dispatch production work.
    # Provider-configured credentials pass through the process environment, not the
    # brief, store, or command arguments. A direct provider CLI is not a fallback.
    up = [name for name in up if name in AGENT_PROVIDERS]

    # A PIN runs on one model only, and waits for it. The provider name is
    # normalized the same way the cascade and prefer paths normalize it.
    pinned = override.get("pin")
    if isinstance(pinned, dict) and pinned.get("provider") and pinned.get("model"):
        return _choice(str(pinned["provider"]).strip().lower(), str(pinned["model"]), tier)

    # An explicit run cascade (`--cascade founder`) overrides soft task preferences
    # baked into a DAG file. Only a hard PIN outranks the run cascade.
    # One entry is skipped while its provider is cooling, or while its own lane key
    # `provider/model` cools: that is how Opus falls back to Neon and Neon to Carbon.
    cascade = (state.get("config") or {}).get("provider_cascade")
    prefer = cascade if (isinstance(cascade, list) and cascade) else override.get("prefer")
    if isinstance(prefer, list) and prefer:
        def lane_burned(want: dict, respect_provider_cooldown: bool) -> bool:
            provider = str(want.get("provider") or "").strip().lower()
            model = want.get("model")
            if model and cooldowns is not None and cooldowns.cooling(f"{provider}/{model}"):
                return True
            return bool(respect_provider_cooldown and cooldowns is not None
                        and cooldowns.cooling(provider))

        for want in prefer:
            if not isinstance(want, dict):
                continue
            provider = str(want.get("provider") or "").strip().lower()
            model = want.get("model")
            if provider and provider in up and not lane_burned(want, True):
                model = model or catalog()["providers"][provider]["models"][tier]
                return _choice(provider, str(model), tier)
        for want in prefer:
            if not isinstance(want, dict):
                continue
            provider = str(want.get("provider") or "").strip().lower()
            model = want.get("model")
            if provider and provider in up and not lane_burned(want, False):
                model = model or catalog()["providers"][provider]["models"][tier]
                return _choice(provider, str(model), tier)
        # None of the preferred providers is usable. Fall through to the pool or the account preference.

    if pool:
        pooled = pool.next(up, tier, cooldowns=cooldowns)
        if pooled is not None:
            return pooled

    healthy = [p for p in up if cooldowns is None or not cooldowns.cooling(p)]
    provider = (healthy or up or ["simulator"])[0]
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
    provider = str(pinned["provider"]).strip().lower()
    if provider in usable(state):
        return None
    return "pinned to %s / %s, which is not available" % (provider, pinned.get("model") or "?")
