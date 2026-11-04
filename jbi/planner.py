"""The planning chat, the import converter and the plan writer.

Every function takes a `Completer`. `chat_reply` and `from_import` let a model error
reach the caller. `finish` never does: it validates the model plan, repairs it once,
and then falls back to a plan built from the PRD features, so the user is never stuck.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import waves
from .llm import Completer, CompletionError, Prompt
from .protocol import TASK_ID, ProtocolError

DETAIL_RULES = {
    "user": "The user gives the details. Ask very little. Summarise what you understood in two or three lines.",
    "guided": "Ask one to three focused questions about the most important unknowns. Summarise first, in one line.",
    "delegate": "Decide the open choices yourself. State each decision in one line. Ask at most one question.",
}
CHAT_SYSTEM = (
    "You are the planning chat of a tool that turns an idea into working software. "
    "Be brief and concrete. Never ask a question just to ask one. "
    "Set ready to true as soon as you know enough to write a plan. "
    'Reply with one JSON object: {"reply": string, "ready": boolean}.\n'
)
PLAN_SYSTEM = (
    "You write the plan for a software project. Reply with one JSON object with the keys "
    "rough_prd (string), prd and tasks.\n"
    "prd has: name, problem, users, features[], constraints[], assumptions[], stack_preferences[], "
    "components[]. Record every choice you made for the user as an assumption. "
    "Each component is {id, name, kind, layer, tech, talks_to:[{to, label, protocol}]}; "
    "kind is one of client, service, worker, database, cache, queue, storage, external.\n"
    "tasks is a list of {id, title, goal, deps[], acceptance, owns[], components[]}. "
    "An id matches ^[a-z][a-z0-9-]{0,39}$. deps name earlier task ids and form no cycle. "
    "acceptance is one shell command that exits non-zero on failure. owns lists relative file paths. "
    "components lists component ids. Small tasks. Maximise tasks that can run in parallel."
)
FALLBACK_ACCEPTANCE = (
    'python3 -c "import json,subprocess,sys;'
    "sys.exit(subprocess.call(json.load(open('.jbi/commands.json'))['test'],shell=True))\""
)
FALLBACK_COMPONENT = {"id": "app", "name": "Application", "kind": "service", "layer": "backend",
                      "tech": "", "talks_to": []}


@dataclass(frozen=True)
class Reply:
    text: str
    ready: bool


@dataclass(frozen=True)
class Draft:
    rough_prd: str
    summary: str


@dataclass(frozen=True)
class Result:
    rough_prd: str
    prd: dict
    tasks: list[dict]


# ---- helpers ---------------------------------------------------------------------------


def _transcript(state: dict) -> str:
    lines = [f"{m['role']}: {m['text']}" for m in state.get("intake", {}).get("messages", [])]
    return "\n".join(lines) or "(no messages yet)"


def _json_object(completion_text: str) -> dict | None:
    """The first JSON object in a completion, or None."""
    from .llm import extract_json
    try:
        value = extract_json(completion_text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def slug(text: str, fallback: str = "item") -> str:
    """A task-id-safe slug: lower case, digits and hyphens, at most 40 characters."""
    words = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    words = re.sub(r"^[^a-z]+", "", words)[:40].rstrip("-")
    return words or fallback


def _strings(value: object) -> list[str]:
    items = value if isinstance(value, list) else []
    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def _feature_name(feature: object) -> str:
    if isinstance(feature, dict):
        feature = feature.get("name") or feature.get("title") or ""
    return str(feature).strip()


# ---- chat and import -------------------------------------------------------------------


def chat_reply(completer: Completer, state: dict) -> Reply:
    detail = state.get("intake", {}).get("detail", "guided")
    prompt = Prompt(system=CHAT_SYSTEM + DETAIL_RULES[detail],
                    user="Conversation so far:\n" + _transcript(state) + "\n\nWrite your next reply.",
                    role="plan", expect_json=True, purpose="planner.chat",
                    context={"detail": detail, "messages": state.get("intake", {}).get("messages", [])})
    completion = completer(prompt)
    data = _json_object(completion.text)
    if data and isinstance(data.get("reply"), str):
        return Reply(data["reply"].strip(), data.get("ready") is True)
    return Reply(completion.text.strip(), False)


def from_import(completer: Completer, text: str) -> Draft:
    prompt = Prompt(
        system=("You read a task list in any form and restate it as a rough product requirements text. "
                "Keep every item. Keep the source order. "
                'Reply with one JSON object: {"rough_prd": string, "summary": string}. '
                "summary is two or three lines for the user."),
        user=text, role="plan", expect_json=True, purpose="planner.import", context={"text": text})
    completion = completer(prompt)
    data = _json_object(completion.text) or {}
    rough = data.get("rough_prd") if isinstance(data.get("rough_prd"), str) else completion.text.strip()
    summary = data.get("summary") if isinstance(data.get("summary"), str) else "I turned your list into a rough PRD."
    return Draft(rough.strip(), summary.strip())


# ---- finish ----------------------------------------------------------------------------


def _component(raw: object, taken: set[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    cid = slug(raw.get("id") or raw.get("name") or "", "")
    if not cid or cid in taken:
        return None
    taken.add(cid)
    links = [{"to": slug(link.get("to", ""), ""), "label": str(link.get("label", "")),
              "protocol": str(link.get("protocol", ""))}
             for link in raw.get("talks_to", []) if isinstance(link, dict)] if isinstance(raw.get("talks_to"), list) else []
    return {"id": cid, "name": str(raw.get("name") or cid), "kind": str(raw.get("kind") or "service"),
            "layer": str(raw.get("layer") or ""), "tech": str(raw.get("tech") or ""), "talks_to": links}


def normalize_prd(raw: object, state: dict) -> dict:
    """A PRD with every key present, so later code never checks for a missing key."""
    raw = raw if isinstance(raw, dict) else {}
    taken: set[str] = set()
    components = [c for c in (_component(item, taken) for item in raw.get("components", []) or []) if c]
    for component in components:
        component["talks_to"] = [link for link in component["talks_to"] if link["to"] in taken]
    first_message = next((m["text"] for m in state.get("intake", {}).get("messages", [])
                          if m["role"] == "user"), "")
    return {
        "name": slug(raw.get("name") or state.get("run", {}).get("name") or first_message, "project"),
        "problem": str(raw.get("problem") or first_message).strip(),
        "users": str(raw.get("users") or "").strip(),
        "features": [name for name in map(_feature_name, raw.get("features", []) or []) if name]
                    or ([first_message.strip()] if first_message.strip() else []),
        "constraints": _strings(raw.get("constraints")), "assumptions": _strings(raw.get("assumptions")),
        "stack_preferences": _strings(raw.get("stack_preferences")),
        "components": components or [dict(FALLBACK_COMPONENT)],
    }


def _task(raw: object, components: set[str]) -> object:
    """Coerce one model task. A value that is not an object stays as it is, so validation names it."""
    if not isinstance(raw, dict):
        return raw
    task = dict(raw)
    for key in ("title", "goal", "acceptance"):
        if isinstance(task.get(key), str):
            task[key] = task[key].strip()
    for key in ("deps", "owns", "components"):
        task[key] = _strings(task.get(key)) if key in task else []
    task["components"] = [c for c in task["components"] if c in components]
    return task


def fallback_plan(prd: dict) -> list[dict]:
    """One shared core task plus one task for each feature. Always valid."""
    component = prd["components"][0]["id"]
    tasks = [{"id": "core", "title": "Build the shared core", "deps": [], "owns": [], "components": [component],
              "goal": f"Build the shared foundation for {prd['name']}: data model, configuration and the "
                      "entry point that every feature uses. Add one smoke test.",
              "acceptance": FALLBACK_ACCEPTANCE}]
    taken = {"core"}
    for feature in prd["features"]:
        tid, number = slug(feature, "feature"), 1
        while tid in taken:
            number += 1
            tid = f"{slug(feature, 'feature')[:36]}-{number}"
        taken.add(tid)
        tasks.append({"id": tid, "title": feature[:80], "deps": ["core"], "owns": [], "components": [component],
                      "goal": f"Implement this feature of {prd['name']}: {feature}. Add tests for it.",
                      "acceptance": FALLBACK_ACCEPTANCE})
    return tasks


def _plan_prompt(state: dict, repair: str | None = None, previous: str = "") -> Prompt:
    user = (f"Conversation:\n{_transcript(state)}\n\nRough PRD so far:\n"
            f"{state.get('plan', {}).get('rough_prd') or '(none)'}")
    if repair:
        user += (f"\n\nYour previous plan was refused:\n{previous}\n\nThe error: {repair}\n"
                 "Fix the error and reply with the complete corrected JSON object.")
    return Prompt(system=PLAN_SYSTEM, user=user, role="plan", expect_json=True,
                  purpose="planner.repair" if repair else "planner.finish",
                  context={"messages": state.get("intake", {}).get("messages", []), "error": repair})


def _try_plan(completer: Completer, prompt: Prompt, state: dict) -> tuple[Result | None, str, str]:
    """Ask once. Returns (result, error, raw_text); result is None when the plan is unusable."""
    text = completer(prompt).text              # a CompletionError reaches finish()
    data = _json_object(text)
    if data is None:
        return None, "The reply held no JSON object.", text
    prd = normalize_prd(data.get("prd"), state)
    components = {c["id"] for c in prd["components"]}
    tasks = [_task(t, components) for t in data["tasks"]] if isinstance(data.get("tasks"), list) else []
    try:
        waves.validate(tasks)
    except (ProtocolError, TypeError, AttributeError) as error:
        return None, getattr(error, "message", str(error)), text
    rough = data.get("rough_prd") if isinstance(data.get("rough_prd"), str) else ""
    return Result(rough or state.get("plan", {}).get("rough_prd") or prd["problem"], prd, tasks), "", text


def finish(completer: Completer, state: dict) -> Result:
    result, error, raw = None, "", ""
    try:
        result, error, raw = _try_plan(completer, _plan_prompt(state), state)
        if result is None:
            result, error, raw = _try_plan(completer, _plan_prompt(state, error, raw), state)
    except CompletionError:
        pass                                    # no model answered: use the deterministic plan
    if result:
        return result
    prd = normalize_prd(None, state)
    prd["assumptions"].append("The plan was derived from the feature list because the model plan was unusable.")
    return Result(state.get("plan", {}).get("rough_prd") or prd["problem"], prd, fallback_plan(prd))
