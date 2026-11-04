"""The one interface between the planning code and any model.

The planner, the stack proposer and the architecture hook never call a provider
directly. They receive a `Completer`: a callable that takes a `Prompt` and returns a
`Completion`. `providers.completer()` builds one from the enabled accounts, and
`simulator.offline_completer()` builds a deterministic one for tests and demos.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    role: str = "plan"                      # the rigor role, used to pick a model tier
    expect_json: bool = False
    purpose: str = ""                       # a stable name, for example "planner.chat"
    context: dict[str, Any] = field(default_factory=dict)   # structured input for offline completers


@dataclass(frozen=True)
class Completion:
    text: str
    model: str = "unknown"
    provider: str = "unknown"
    tokens_in: int | None = None            # None means the provider did not report the value
    tokens_out: int | None = None
    cost_usd: float | None = None

    def json(self) -> Any:
        """Parse the first JSON object or array in the text."""
        return extract_json(self.text)


class Completer(Protocol):
    def __call__(self, prompt: Prompt) -> Completion: ...


class CompletionError(RuntimeError):
    """No enabled provider could answer the prompt."""


def extract_json(text: str) -> Any:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("the completion holds no JSON value")
