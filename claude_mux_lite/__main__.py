"""Run one leased agent dispatch. No credential or header map enters the spec file."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from .harness import LiteHarness, redact, strip_marks
from ..jbi.store import now
from ..jbi.tracking import Tracking


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: python -m claude_mux_lite SPEC.json", file=sys.stderr)
        return 2
    spec = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
    tracking = Tracking(Path(spec["tracking"]))
    history: list[dict] = []
    usage_path = Path(spec["usage_path"])
    try:
        if spec.get("continuation"):
            history = tracking.session_history(spec["session_id"])
        harness = LiteHarness(
            workdir=Path(spec["workdir"]), model=spec["model"], task_id=spec["task_id"],
            tracking=tracking, owns=tuple(spec["owns"]), session=history,
            receipt=Path(spec["receipt"]) if spec.get("receipt") else None,
            checkpoint=Path(spec["checkpoint"]), timeout_sec=float(spec.get("timeout_sec", 1800)),
            max_turns=int(spec.get("max_turns", 200)), effort=spec.get("effort", "high"),
            repo_context=spec.get("repo_context", ""), frozen_contracts=spec.get("frozen_contracts", ""),
        )
        result = harness.run(spec["brief"])
        history = harness.take_messages() if spec.get("continuation") else []
        dispatch = int(spec["dispatch_id"])
        if spec.get("continuation"):
            tracking.record_cost(dispatch, input_tokens=result.usage.input_tokens,
                                 output_tokens=result.usage.output_tokens, cache_read=result.usage.cache_read,
                                 cache_write=result.usage.cache_write)
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
        if spec.get("continuation") and history:
            tracking.record_session_history(spec["session_id"], strip_marks(history))
        tracking.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
