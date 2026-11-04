"""The wire contract: command catalog, payload validation, errors, event types.

`docs/PROTOCOL.md` is the human-readable version of this module.
`catalog()` is the machine-readable version; the server serves it at /api/v1/schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import PROTOCOL_VERSION

PHASES = ("intake", "stack", "bootstrap", "configure", "execute", "complete")
RIGOR_LEVELS = ("vibe", "medium", "high", "xhigh")
PROFILES = ("max", "balanced", "economy", "micro")
DETAIL_LEVELS = ("user", "guided", "delegate")
STOP_MODES = ("quit", "standdown", "finish", "kill")
TASK_STATUSES = ("blocked", "ready", "running", "verifying", "queued",
                 "merging", "merged", "failed", "paused", "decision", "skipped")
TASK_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
COMMAND_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
ANY_PHASE = PHASES


class ProtocolError(Exception):
    """A request that the protocol refuses. Carries the HTTP status and a stable code."""

    STATUS = {
        "payload_invalid": 400, "command_unknown": 400, "unauthenticated": 401,
        "origin_rejected": 403, "not_found": 404, "protocol_unsupported": 406,
        "phase_invalid": 409, "conflict": 409, "media_unsupported": 415,
        "too_large": 413, "internal": 500,
    }

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details
        self.status = self.STATUS[code]

    def body(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"protocol": PROTOCOL_VERSION, "error": error}


@dataclass(frozen=True)
class Field:
    kind: str                       # "string" | "bool" | "enum" | "task"
    required: bool = True
    choices: tuple[str, ...] = ()
    max_length: int = 20000
    secret: bool = False

    def describe(self) -> dict:
        out: dict[str, Any] = {"type": self.kind, "required": self.required}
        if self.choices:
            out["enum"] = list(self.choices)
        if self.kind in ("string", "task"):
            out["maxLength"] = self.max_length if self.kind == "string" else 40
        if self.secret:
            out["secret"] = True
        return out


@dataclass(frozen=True)
class Command:
    type: str
    phases: tuple[str, ...]
    summary: str
    fields: dict[str, Field] = field(default_factory=dict)


def _enum(choices: tuple[str, ...], required: bool = True) -> Field:
    return Field("enum", required, choices)


_TEXT = Field("string", max_length=20000)
_TASK = Field("task")

COMMANDS: dict[str, Command] = {c.type: c for c in (
    Command("intake.import", ("intake",), "Import a task list in any format.", {"text": _TEXT}),
    Command("intake.say", ("intake",), "Send one message to the planning chat.", {"text": _TEXT}),
    Command("intake.detail", ("intake",), "Choose how much detail the user gives.",
            {"level": _enum(DETAIL_LEVELS)}),
    Command("intake.finish", ("intake",), "Stop questions and produce the plan."),
    Command("stack.revise", ("stack",), "Ask for changes to the proposed stack.",
            {"request": Field("string", max_length=4000)}),
    Command("stack.accept", ("stack",), "Accept the stack and start bootstrap.",
            {"target": Field("string", max_length=1024),
             "name": Field("string", required=False, max_length=80)}),
    Command("stack.back", ("stack", "bootstrap"),
            "Return to intake from stack, or to stack after a failed bootstrap."),
    Command("bootstrap.retry", ("bootstrap",), "Run bootstrap again after it failed."),
    Command("run.configure", ("configure", "execute"), "Set the rigor level and model profile.",
            {"rigor": _enum(RIGOR_LEVELS), "profile": _enum(PROFILES)}),
    Command("run.start", ("configure",), "Start the build."),
    Command("task.override", ("configure", "execute"), "Override rigor, profile or model for one task.",
            {"task": _TASK, "rigor": _enum(RIGOR_LEVELS, False), "profile": _enum(PROFILES, False),
             "model": Field("string", required=False, max_length=120)}),
    Command("task.retry", ("execute",), "Retry a failed or paused task.", {"task": _TASK}),
    Command("task.pause", ("execute",), "Pause a task after its current stage.", {"task": _TASK}),
    Command("task.resume", ("execute",), "Resume a paused task.", {"task": _TASK}),
    Command("decision.answer", ANY_PHASE, "Answer an open decision.",
            {"decision": Field("string", max_length=64), "answer": Field("string", max_length=4000)}),
    Command("account.set", ANY_PHASE, "Enable or disable a provider, optionally with an API key.",
            {"provider": Field("string", max_length=40), "enabled": Field("bool"),
             "api_key": Field("string", required=False, max_length=400, secret=True)}),
    Command("account.probe", ANY_PHASE, "Run the live probe for one provider.",
            {"provider": Field("string", max_length=40)}),
    Command("architecture.refresh", ("configure", "execute", "complete"),
            "Run the architecture.sync hook now."),
    Command("run.stop", ANY_PHASE, "Stop the run with one of four modes.", {"mode": _enum(STOP_MODES)}),
)}

EVENTS: dict[str, str] = {
    "run.created": "The run document exists.",
    "run.phase": "The phase changed.",
    "run.config": "Rigor, profile or limits changed.",
    "run.stopping": "A stop mode started.",
    "chat.message": "A chat message was added.",
    "plan.updated": "The rough PRD, PRD or plan changed.",
    "stack.proposed": "A stack proposal is ready.",
    "bootstrap.status": "The bootstrap step changed.",
    "task.status": "A task status changed.",
    "task.stage": "A task stage started, passed or failed.",
    "task.override": "A per-task override changed.",
    "session.status": "An agent session started, changed or ended.",
    "lane.status": "A CI lane changed.",
    "decision.opened": "The run needs an answer from the user.",
    "decision.answered": "The user answered a decision.",
    "architecture.updated": "The architecture graph changed.",
    "telemetry.sample": "A usage or timing sample was recorded.",
    "account.status": "A provider account or probe result changed.",
    "log.line": "An activity line for the timeline.",
}


def validate_command(body: Any) -> tuple[str, str, dict]:
    """Return (id, type, payload) for a valid command body, else raise ProtocolError."""
    if not isinstance(body, dict):
        raise ProtocolError("payload_invalid", "The body must be a JSON object.")
    cid, ctype, payload = body.get("id"), body.get("type"), body.get("payload", {})
    if not isinstance(cid, str) or not COMMAND_ID.match(cid):
        raise ProtocolError("payload_invalid", "Field 'id' must be 1-64 characters of [A-Za-z0-9_.:-].")
    if ctype not in COMMANDS:
        raise ProtocolError("command_unknown", f"Unknown command type {ctype!r}.", known=sorted(COMMANDS))
    if not isinstance(payload, dict):
        raise ProtocolError("payload_invalid", "Field 'payload' must be a JSON object.")
    return cid, ctype, validate_payload(COMMANDS[ctype], payload)


def validate_payload(command: Command, payload: dict) -> dict:
    unknown = sorted(set(payload) - set(command.fields))
    if unknown:
        raise ProtocolError("payload_invalid", f"{command.type} does not take field(s) {', '.join(unknown)}.",
                            unknown=unknown)
    clean: dict[str, Any] = {}
    for name, spec in command.fields.items():
        if name not in payload or payload[name] is None:
            if spec.required:
                raise ProtocolError("payload_invalid", f"{command.type} needs field '{name}'.", field=name)
            continue
        clean[name] = _check(command.type, name, spec, payload[name])
    return clean


def _check(ctype: str, name: str, spec: Field, value: Any) -> Any:
    def fail(why: str) -> ProtocolError:
        return ProtocolError("payload_invalid", f"{ctype}.{name} {why}.", field=name)

    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise fail("must be true or false")
        return value
    if not isinstance(value, str):
        raise fail("must be a string")
    if spec.kind == "enum" and value not in spec.choices:
        raise fail(f"must be one of {', '.join(spec.choices)}")
    if spec.kind == "task" and not TASK_ID.match(value):
        raise fail("must be a task id")
    if spec.kind == "string":
        value = value.strip()
        if not value and spec.required:
            raise fail("must not be empty")
        if len(value) > spec.max_length:
            raise fail(f"must be {spec.max_length} characters or fewer")
    return value


def redact(ctype: str, payload: dict) -> dict:
    """Return a copy of a payload with every secret field removed, for the command log."""
    spec = COMMANDS[ctype].fields
    return {k: ("<redacted>" if spec.get(k, Field("string")).secret else v) for k, v in payload.items()}


def catalog() -> dict:
    return {
        "protocol": PROTOCOL_VERSION,
        "phases": list(PHASES),
        "rigor": list(RIGOR_LEVELS),
        "profiles": list(PROFILES),
        "stop_modes": list(STOP_MODES),
        "task_statuses": list(TASK_STATUSES),
        "commands": {c.type: {"phases": list(c.phases), "summary": c.summary,
                              "payload": {n: f.describe() for n, f in c.fields.items()}}
                     for c in COMMANDS.values()},
        "events": dict(EVENTS),
    }


def initial_state(run_id: str, created: str, *, name: str, mode: str, target: str,
                  max_sessions: int, lanes: int, tmux: str) -> dict:
    """The state document of a new run. Every client renders this shape."""
    return {
        "run": {"id": run_id, "name": name, "phase": "intake", "mode": mode, "target": target,
                "created": created, "stopping": None, "busy": None, "tmux": tmux},
        "config": {"rigor": "high", "profile": "balanced", "max_sessions": max_sessions, "lanes": lanes},
        "accounts": {},
        "intake": {"detail": "guided", "messages": []},
        "plan": {"revision": 0, "rough_prd": "", "prd": {}, "tasks": {}, "order": [], "waves": []},
        "stack": {"layers": [], "summary": "", "accepted": False},
        "bootstrap": {"status": "pending", "detail": "", "commit": None},
        "sessions": {},
        "lanes": {},
        "decisions": {},
        "architecture": {"revision": 0, "nodes": {}, "edges": {}},
        "telemetry": {"tokens_in": 0, "tokens_out": 0, "cost_usd": None, "samples": 0},
        "timeline": [],
    }
