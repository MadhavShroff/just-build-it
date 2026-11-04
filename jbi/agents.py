"""Agent command lines, child environments and usage parsing, per provider.

Every command is an argument list. The prompt only names the brief file, so no
secret and no task text reaches a command line beyond the brief path.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Variables every child may keep. Everything else is dropped, so one provider's
# secret never reaches another provider's agent or a plain command.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TZ", "TMPDIR",
            "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMUX_TMPDIR")
BASE_PREFIXES = ("LC_",)
PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR"),
    "codex": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME"),
    "agy": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"),
    "openai-compatible": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "simulator": ("JBI_SIM_FAIL", "JBI_SIM_SLOW"),
}


@dataclass(frozen=True)
class AgentJob:
    """Everything needed to start one agent for one stage."""
    provider: str
    model: str
    task: str
    role: str
    cwd: Path
    brief: Path
    receipt: Path
    attempt: int = 0
    owns: tuple[str, ...] = ()


def prompt(job: AgentJob) -> str:
    return (f"Read the brief at {job.brief} and follow it exactly. "
            f"Write the receipt file {job.receipt} last.")


def command(job: AgentJob) -> list[str]:
    """The argument list that starts the agent for `job`."""
    text = prompt(job)
    if job.provider == "claude":
        return ["claude", "-p", "--model", job.model, "--permission-mode", "acceptEdits",
                "--output-format", "stream-json", "--verbose", text]
    if job.provider == "codex":
        return ["codex", "exec", "-m", job.model, "--json", "--skip-git-repo-check",
                "-s", "workspace-write", text]
    if job.provider == "agy":
        return ["agy", "-p", "--model", job.model, "--output-format", "stream-json", text]
    if job.provider == "simulator":
        argv = [sys.executable, "-m", "jbi.simulator", "--task", job.task, "--role", job.role,
                "--cwd", str(job.cwd), "--receipt", str(job.receipt), "--attempt", str(job.attempt)]
        return argv + (["--owns", job.owns[0]] if job.owns else [])
    raise ValueError(f"provider {job.provider!r} has no agent command")


def extra_env(provider: str) -> dict[str, str]:
    """Non-secret variables a provider's agent needs in addition to the inherited ones."""
    return {"PYTHONPATH": str(PACKAGE_ROOT)} if provider == "simulator" else {}


def child_env(provider: str | None, source: Mapping[str, str] | None = None,
              extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """An environment for a child: the base allowlist, the provider's own variables, `extra`."""
    source = os.environ if source is None else source
    allowed = set(BASE_ENV) | set(PROVIDER_ENV.get(provider or "", ()))
    env = {k: v for k, v in source.items() if k in allowed or k.startswith(BASE_PREFIXES)}
    env.update(extra or {})
    return env


def runtime_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for the tmux server: the union of every provider allowlist."""
    source = os.environ if source is None else source
    allowed = set(BASE_ENV).union(*PROVIDER_ENV.values())
    return {k: v for k, v in source.items() if k in allowed or k.startswith(BASE_PREFIXES)}


@dataclass
class Usage:
    """Token and cost totals for one session. None means the provider did not report it."""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    _seen: bool = field(default=False, repr=False)

    def add(self, tokens_in: int | None, tokens_out: int | None, cost: float | None) -> None:
        if tokens_in is not None:
            self.tokens_in = (self.tokens_in or 0) + tokens_in
        if tokens_out is not None:
            self.tokens_out = (self.tokens_out or 0) + tokens_out
        if cost is not None:
            self.cost_usd = (self.cost_usd or 0.0) + cost
        self._seen = True

    def as_dict(self) -> dict | None:
        if not self._seen:
            return None
        return {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "cost_usd": self.cost_usd}


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _sum(usage: dict, *names: str) -> int | None:
    values = [_int(usage.get(n)) for n in names]
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def parse_usage(provider: str, line: str, usage: Usage) -> None:
    """Add the usage that one stream-json line reports, if it reports any."""
    line = line.strip()
    if not line.startswith("{"):
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    kind = event.get("type")
    if provider in ("claude", "simulator", "agy") and kind == "result":
        data = event.get("usage") or event.get("stats") or {}
        if isinstance(data, dict):
            cost = event.get("total_cost_usd")
            usage.add(_sum(data, "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"),
                      _sum(data, "output_tokens"), cost if isinstance(cost, (int, float)) else None)
    elif provider == "codex" and kind == "turn.completed":
        data = event.get("usage") or {}
        if isinstance(data, dict):
            usage.add(_sum(data, "input_tokens", "cached_input_tokens"), _sum(data, "output_tokens"), None)
