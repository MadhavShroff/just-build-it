"""Run one leased agent dispatch. No credential or header map enters the spec file."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from .harness import LiteHarness, strip_marks
from jbi.store import now
from jbi.tracking import Tracking


def bootstrap(spec_path: str) -> int:
    """Read the real credentials once, start the private broker, then exec the neutral child.

    This mode never opens the spec contents and never touches tracking."""
    from . import credentials
    try:
        extra = credentials.start_broker(Path(spec_path).resolve().parent, os.environ)
    except credentials.BrokerError as error:
        print(f"credential broker failed: {error}", file=sys.stderr)
        return 3
    credentials.exec_child(spec_path, os.environ, extra)
    return 3                                    # not reached: exec replaced this process


def main(arguments: list[str]) -> int:
    if len(arguments) == 2 and arguments[0] == "--bootstrap":
        return bootstrap(arguments[1])
    if len(arguments) != 1:
        print("usage: python -m claude_mux_lite [--bootstrap] SPEC.json", file=sys.stderr)
        return 2
    spec = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
    tracking_path = Path(spec["tracking"])
    usage_path = Path(spec["usage_path"])
    if not tracking_path.is_file():
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        usage_path.write_text(json.dumps({"exit_code": 3,
                                         "final_text": "internal store is missing", "ended_at": now()}))
        return 3
    tracking = Tracking(tracking_path)
    history: list[dict] = []
    usage_path = Path(spec["usage_path"])
    try:
        history_key = spec.get("history_session_id") or spec["session_id"]
        if spec.get("continuation"):
            history = tracking.session_history(history_key)
        def inbox() -> str:
            text = []
            for message in tracking.pending_messages(spec["session_id"]):
                text.append(f"Message from {message['sender']}: {message['text']}")
                tracking.message_receipt(message["id"], "delivered", "injected before the next model turn")
            return "\n".join(text).strip()

        harness = LiteHarness(
            workdir=Path(spec["workdir"]), model=spec["model"], task_id=spec["task_id"],
            tracking=tracking, owns=tuple(spec["owns"]), session=history,
            receipt=Path(spec["receipt"]) if spec.get("receipt") else None,
            checkpoint=Path(spec["checkpoint"]), timeout_sec=float(spec.get("timeout_sec", 1800)), before_turn=inbox,
            max_turns=int(spec.get("max_turns", 200)), effort=spec.get("effort", "high"),
            repo_context=spec.get("repo_context", ""), frozen_contracts=spec.get("frozen_contracts", ""),
        )
        result = harness.run(spec["brief"])
        history = harness.take_messages()
        dispatch = int(spec["dispatch_id"])
        tracking.record_cost(dispatch, input_tokens=result.usage.input_tokens,
                             output_tokens=result.usage.output_tokens, cache_read=result.usage.cache_read,
                             cache_write=result.usage.cache_write)
        if history:
            tracking.record_session_history(history_key, strip_marks(history), status="idle")
        usage = {"dispatch_id": dispatch, "input_tokens": result.usage.input_tokens,
                 "output_tokens": result.usage.output_tokens, "cache_read": result.usage.cache_read,
                 "cache_write": result.usage.cache_write, "exit_code": result.exit_code,
                 "final_text": result.final_text, "turns": result.turns, "ended_at": now()}
        usage_path.write_text(json.dumps(usage, separators=(",", ":")))
        return result.exit_code
    except sqlite3.Error as error:
        usage_path.write_text(json.dumps({"dispatch_id": spec["dispatch_id"], "exit_code": 3,
                                         "final_text": f"internal store error: {error}", "ended_at": now()}))
        return 3
    finally:
        tracking.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
