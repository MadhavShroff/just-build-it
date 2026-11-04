"""An offline agent and an offline completer, for demos and tests.

`python3 -m jbi.simulator --task T --role R --cwd D --receipt P [--attempt N] [--owns PATH]`

- implement writes one small file under the task's first owned path (or src/<task>.txt)
  and commits nothing: the supervisor commits.
- every role prints a few progress lines, one stream-json usage line, and a receipt.
- JBI_SIM_FAIL=<task>:<role>:<n>[,...] makes the first n attempts of that stage FAIL.
- JBI_SIM_SLOW=<seconds> sleeps that long between progress lines (default 0.05).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .llm import Completion, Prompt

STEPS = ("reading the brief", "working", "writing the receipt")


def planned_failures(task: str, role: str, env: str | None = None) -> int:
    """How many attempts of (task, role) must FAIL, from JBI_SIM_FAIL."""
    for item in filter(None, (env if env is not None else os.environ.get("JBI_SIM_FAIL", "")).split(",")):
        parts = item.strip().split(":")
        if len(parts) == 3 and parts[0] == task and parts[1] == role and parts[2].isdigit():
            return int(parts[2])
    return 0


def target_file(cwd: Path, task: str, owns: str | None) -> Path:
    if not owns:
        return cwd / "src" / f"{task}.txt"
    path = cwd / owns
    return path if path.suffix else path / f"{task}.txt"


SIM_COMMANDS = {
    "build": "python3 -m compileall -q src",
    "test": "python3 -m unittest discover -s tests -q",
    "lint": "python3 -m compileall -q src tests",
    "format": "true",
    "format_check": "true",
}


def bootstrap_repo(cwd: Path) -> None:
    """Set up a minimal Python project with real build, lint and test commands. No feature code."""
    (cwd / ".jbi").mkdir(parents=True, exist_ok=True)
    (cwd / ".jbi" / "commands.json").write_text(json.dumps(SIM_COMMANDS, indent=2) + "\n")
    (cwd / "src").mkdir(exist_ok=True)
    (cwd / "src" / "__init__.py").write_text("")
    (cwd / "tests").mkdir(exist_ok=True)
    (cwd / "tests" / "__init__.py").write_text("")
    (cwd / "tests" / "test_smoke.py").write_text(
        "import unittest\n\n\nclass Smoke(unittest.TestCase):\n"
        "    def test_package_imports(self):\n        import src  # noqa: F401\n\n\n"
        "if __name__ == \"__main__\":\n    unittest.main()\n")
    (cwd / ".gitignore").write_text("__pycache__/\n*.pyc\n")


def run(task: str, role: str, cwd: Path, receipt: Path, attempt: int, owns: str | None) -> int:
    pause = float(os.environ.get("JBI_SIM_SLOW", "0.05"))
    for step in STEPS:
        print(f"[sim {task}:{role}] {step}", flush=True)
        time.sleep(pause)
    if role == "bootstrap":
        bootstrap_repo(cwd)
    elif role == "implement":
        path = target_file(cwd, task, owns)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{task} attempt {attempt + 1}\n")
    passed = attempt >= planned_failures(task, role)
    print(json.dumps({"type": "result", "usage": {"input_tokens": 120, "output_tokens": 40}}), flush=True)
    finding = "simulated result" if passed else f"simulated failure on attempt {attempt + 1}"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(f"BRIEF {task} | uncommitted | {'PASS' if passed else 'FAIL'}\n{finding}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jbi.simulator")
    parser.add_argument("--task", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--attempt", type=int, default=0)
    parser.add_argument("--owns")
    args = parser.parse_args(argv)
    return run(args.task, args.role, args.cwd, args.receipt, args.attempt, args.owns)


# ---- the offline completer --------------------------------------------------------------

NOT_FEATURES = ("python", "sqlite", "postgres", "node", "react", "hosting", "cheap", "budget",
                "free", "deploy", "stack", "framework", "language", "database")

# Each guided question, and the words that show the user already answered it.
GUIDED_QUESTIONS = (
    ("Who uses it: only you, a small group, or the public?",
     ("me ", "only me", "myself", "friends", "team", "public", "everyone", "users", "family")),
    ("Which features matter most for the first version?", (",", " and ")),
    ("Any limit on hosting or budget, or a stack you want?", NOT_FEATURES),
)


def _user_text(context: dict) -> list[str]:
    return [m["text"] for m in context.get("messages", []) if m.get("role") == "user"]



def _features(texts: list[str]) -> list[str]:
    """Split the user's messages into feature phrases: clauses joined by commas or 'and'.

    A clause about the stack, hosting or budget is a constraint, not a feature."""
    found: list[str] = []
    for text in texts:
        head = text.split(":", 1)[-1] if ":" in text else text
        for sentence in head.replace(";", ".").split("."):
            for part in sentence.replace(" and ", ",").split(","):
                phrase = " ".join(part.split()).strip(" -")
                lowered = phrase.lower()
                if not 2 <= len(phrase.split()) <= 12 or any(word in lowered for word in NOT_FEATURES):
                    continue
                if lowered not in map(str.lower, found):
                    found.append(phrase[0].upper() + phrase[1:])
    return found[:8]


def _offline_plan(context: dict) -> dict:
    """A deterministic PRD and DAG from the conversation: a core, then one task per feature."""
    texts = _user_text(context)
    features = _features(texts) or ["The main workflow"]
    components = [
        {"id": "web", "name": "Web app", "kind": "client", "layer": "frontend", "tech": "HTML",
         "talks_to": [{"to": "api", "label": "requests", "protocol": "HTTP"}]},
        {"id": "api", "name": "API", "kind": "service", "layer": "backend", "tech": "Python",
         "talks_to": [{"to": "db", "label": "reads and writes", "protocol": "SQL"}]},
        {"id": "db", "name": "Database", "kind": "database", "layer": "data", "tech": "SQLite", "talks_to": []},
    ]
    tasks = [{"id": "core", "title": "Build the data model and app skeleton", "deps": [],
              "goal": "Create the data model, configuration and the entry point that every feature uses.",
              "acceptance": "python3 -m unittest discover -s tests -q", "owns": ["src/core"],
              "components": ["api", "db"]}]
    for index, feature in enumerate(features, start=1):
        tid = "-".join(feature.lower().split()[:4])
        tid = "".join(ch for ch in tid if ch.isalnum() or ch == "-")[:36].strip("-") or f"feature-{index}"
        tasks.append({"id": tid if tid[0].isalpha() else f"f-{tid}", "title": feature, "deps": ["core"],
                      "goal": f"Implement: {feature}.", "acceptance": "python3 -m unittest discover -s tests -q",
                      "owns": [f"src/{tid}"], "components": ["api", "web"]})
    tasks.append({"id": "polish", "title": "Connect the features in the web app",
                  "deps": [t["id"] for t in tasks[1:]], "goal": "Link every feature into one navigable app.",
                  "acceptance": "python3 -m unittest discover -s tests -q", "owns": ["src/polish"],
                  "components": ["web"]})
    first = texts[0] if texts else "A new app"
    return {"rough_prd": "\n".join(f"- {f}" for f in features),
            "prd": {"name": "", "problem": first, "users": "", "features": features, "constraints": [],
                    "assumptions": ["The offline planner derived the plan from the conversation."],
                    "stack_preferences": [], "components": components},
            "tasks": tasks}


def _offline_chat(context: dict) -> dict:
    """Ask the next guided question that neither the user nor an earlier reply has covered."""
    messages = context.get("messages", [])
    said = " ".join(m["text"].lower() for m in messages if m.get("role") == "user") + " "
    asked = {m["text"] for m in messages if m.get("role") == "assistant"}
    if context.get("detail") == "user":
        return {"reply": "Noted. Add anything else, or select Stop questions and plan.", "ready": True}
    if context.get("detail") != "delegate":
        for question, answered_by in GUIDED_QUESTIONS:
            if question not in asked and not any(word in said for word in answered_by):
                return {"reply": question, "ready": False}
    return {"reply": "I have enough to plan. I will choose the remaining details and record them "
                     "as assumptions. Select Stop questions and plan.", "ready": True}


def _answer(prompt: Prompt) -> str:
    context = prompt.context or {}
    if "fallback" in context:
        return json.dumps(context["fallback"])
    if prompt.purpose == "planner.chat":
        return json.dumps(_offline_chat(context))
    if prompt.purpose in ("planner.finish", "planner.repair"):
        return json.dumps(_offline_plan(context))
    if prompt.purpose == "planner.import":
        text = context.get("text", "")
        items = [line.strip(" -*\t") for line in text.splitlines() if line.strip(" -*\t")]
        return json.dumps({"rough_prd": "\n".join(f"- {i}" for i in items),
                           "summary": f"I read {len(items)} items into a rough PRD."})
    return json.dumps({})          # stack and architecture use their own deterministic defaults


def offline_completer():
    """A deterministic jbi.llm.Completer that answers by Prompt.purpose.

    A prompt that carries `context["fallback"]` gets that object back as JSON, so the
    planning modules decide the offline answer themselves."""
    def complete(prompt: Prompt) -> Completion:
        text = _answer(prompt)
        return Completion(text=text, model="sim-s", provider="simulator",
                          tokens_in=len(prompt.user) // 4, tokens_out=len(text) // 4)
    return complete


if __name__ == "__main__":
    sys.exit(main())
