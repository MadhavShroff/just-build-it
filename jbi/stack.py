"""The stack proposal: one cheapest-workable choice for each layer.

The model proposes; this module validates. A stack that the user named in the chat or
in a revision request always wins. When the model output does not parse, a fixed table
supplies the proposal, so the user always sees one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .llm import Completer, CompletionError, Prompt

COSTS = ("free", "low", "paid")
# Cheapest first. Each option is (choice, reason, cost).
OPTIONS: dict[str, list[tuple[str, str, str]]] = {
    "language": [("Python 3.12", "Ships on most machines, no build step", "free"),
                 ("TypeScript", "One language for server and browser", "free"),
                 ("Go", "Single static binary", "free")],
    "database": [("SQLite", "A file on disk, no server to run", "free"),
                 ("PostgreSQL", "Relational features for growth", "low"),
                 ("MongoDB", "Document store for loose shapes", "low")],
    "frontend": [("htmx", "Server-rendered pages, no build step", "free"),
                 ("React", "Rich client interactions", "free"),
                 ("Svelte", "Small client bundle", "free")],
    "hosting": [("Local machine", "Nothing to pay while you build", "free"),
                ("Fly.io free tier", "Free small deploy", "free"),
                ("AWS", "Full cloud services", "paid")],
}
LAYER_ORDER = tuple(OPTIONS)
SYSTEM = (
    "You choose a tech stack for a software project. Prefer the cheapest workable option for every "
    "layer: free tiers, local databases, no paid service by default. A stack the user named wins. "
    'Reply with one JSON object: {"layers": [{"layer": string, "choice": string, "reason": string, '
    '"cost": "free" | "low" | "paid"}]}. Use one short reason. Layers: language, database, frontend, hosting.'
)


@dataclass(frozen=True)
class Proposal:
    layers: list[dict]

    @property
    def summary(self) -> str:
        return " · ".join(layer["choice"] for layer in self.layers)

    def as_state(self) -> dict:
        return {"layers": [dict(layer) for layer in self.layers], "summary": self.summary, "accepted": False}


def _layer(layer: str, choice: str, reason: str, cost: str) -> dict:
    return {"layer": layer, "choice": choice, "reason": reason, "cost": cost}


def default_layers() -> list[dict]:
    return [_layer(name, *options[0]) for name, options in OPTIONS.items()]


def named_choices(*texts: str) -> dict[str, tuple[str, str, str]]:
    """Known options that the user wrote by name, keyed by layer. The last mention wins."""
    found: dict[str, tuple[str, str, str]] = {}
    for text in texts:
        for layer, options in OPTIONS.items():
            for option in options:
                words = re.escape(option[0].split(" ")[0] if option[0].startswith("Python") else option[0])
                if re.search(rf"(?<![\w]){words}(?![\w])", text, re.I):
                    found[layer] = option
    return found


def _parse(text: str) -> list[dict] | None:
    from .llm import extract_json
    try:
        data = extract_json(text)
    except ValueError:
        return None
    raw = data.get("layers") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return None
    layers = []
    for item in raw:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip()
                                                 for k in ("layer", "choice")):
            return None
        cost = item.get("cost") if item.get("cost") in COSTS else "low"
        layers.append(_layer(item["layer"].strip(), item["choice"].strip(), str(item.get("reason", "")).strip(), cost))
    return layers or None


def _apply_named(layers: list[dict], named: dict[str, tuple[str, str, str]]) -> list[dict]:
    by_layer = {layer["layer"]: layer for layer in layers}
    for name, (choice, _reason, cost) in named.items():
        by_layer[name] = _layer(name, choice, "You named this choice", cost)
    ordered = [by_layer.pop(name) for name in LAYER_ORDER if name in by_layer]
    return ordered + list(by_layer.values())


def propose(completer: Completer, prd: dict, messages: list[dict], request: str | None = None,
            current: dict | None = None) -> Proposal:
    user_texts = [m["text"] for m in messages if m.get("role") == "user"]
    named = named_choices(*user_texts, *([request] if request else []))
    lines = [f"Project: {prd.get('name', '')}. Problem: {prd.get('problem', '')}",
             "Features: " + "; ".join(prd.get("features", [])),
             "Stack preferences: " + "; ".join(prd.get("stack_preferences", [])),
             "User messages:\n" + "\n".join(user_texts)]
    if current:
        lines.append("Current proposal: " + current.get("summary", ""))
    if request:
        lines.append("Change requested: " + request)
    prompt = Prompt(system=SYSTEM, user="\n".join(lines), role="plan", expect_json=True, purpose="stack.propose",
                    context={"prd": prd, "request": request, "current": current})
    try:
        layers = _parse(completer(prompt).text)
    except CompletionError:
        layers = None
    if layers is None:
        layers = [dict(layer) for layer in (current or {}).get("layers", [])] or default_layers()
    return Proposal(_apply_named(layers, named))
