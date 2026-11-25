"""Run one leased agent dispatch. No credential or header map enters the spec file."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from .harness import RECEIPT_LINE, LiteHarness, strip_marks
from jbi.store import now
from jbi.tracking import Tracking

_READONLY_RECEIPT_RE = re.compile(
    r"-(?:review(?:-[a-z0-9-]+)?|verify|security|test-audit|methodology-audit|dry-run)-\d+\.txt$"
)


def _is_brief_turn(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def _continuation_messages(messages: list[dict]) -> tuple[list[dict], str]:
    """Keep complete exchanges; cap multi-attempt history to the latest attempt."""
    kept = list(messages)
    discarded: list[str] = []
    while kept:
        last = kept[-1]
        content = last.get("content")
        if last.get("role") == "assistant":
            blocks = content if isinstance(content, list) else []
            if any(block.get("type") == "tool_use" for block in blocks if isinstance(block, dict)):
                discarded.append("an unanswered tool call")
                kept.pop()
                continue
            break
        if last.get("role") == "user":
            blocks = content if isinstance(content, list) else []
            if not any(block.get("type") == "tool_result" for block in blocks if isinstance(block, dict)):
                discarded.append("the incomplete previous turn")
                kept.pop()
                continue
        break
    if len(kept) > 4 and kept[-1].get("role") == "assistant":
        last_blocks = kept[-1].get("content")
        last_texts = ([last_blocks] if isinstance(last_blocks, str)
                      else [str(b.get("text") or "") for b in (last_blocks or [])
                            if isinstance(b, dict) and b.get("type") == "text"])
        first_line = "\n".join(t for t in last_texts if t.strip()).strip().splitlines()
        if first_line and RECEIPT_LINE.match(first_line[0]) and first_line[0].rstrip().endswith("| PASS"):
            kept = []
    if len(kept) > 30:
        brief_indices = [idx for idx, m in enumerate(kept) if _is_brief_turn(m)]
        if len(brief_indices) > 1:
            kept = kept[brief_indices[-1]:]
    if not discarded:
        return kept, ""
    return kept, ("The previous attempt ended before a valid reply. Its incomplete exchange is omitted. "
                  "Continue the same task with this correction: " + " and ".join(discarded) + ".\n\n")


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
        print("internal store is missing", file=sys.stderr, flush=True)
        return 3
    tracking = Tracking(tracking_path)
    history: list[dict] = []
    usage_path = Path(spec["usage_path"])
    try:
        receipt_name = Path(spec["receipt"]).name if spec.get("receipt") else ""
        is_readonly_stage = (spec.get("role") in ("review", "verify", "security")
                             or bool(_READONLY_RECEIPT_RE.search(receipt_name)))
        history_key = spec.get("history_session_id") or spec["session_id"]
        if spec.get("continuation") and not is_readonly_stage:
            history, correction_note = _continuation_messages(tracking.session_history(history_key))
        else:
            correction_note = ""
        timeout_sec = float(spec.get("timeout_sec", 1800))
        max_turns = int(spec.get("max_turns", 200))
        if is_readonly_stage:
            timeout_sec = min(timeout_sec, 600.0)
            max_turns = min(max_turns, 22)
        def inbox() -> str:
            text = []
            for message in tracking.pending_messages(spec["session_id"]):
                text.append(f"Message from {message['sender']}: {message['text']}")
                tracking.message_receipt(message["id"], "delivered", "injected before the next model turn")
            return "\n".join(text).strip()

        import threading
        def progress(msg: str) -> None:
            print(msg, flush=True)

        stop_beat = threading.Event()
        def beat() -> None:
            while not stop_beat.wait(45.0):
                print(f"[jbi.lite] task={spec['task_id']} model={spec['model']} active...", flush=True)

        beat_thread = threading.Thread(target=beat, daemon=True)
        beat_thread.start()
        try:
            harness = LiteHarness(
                workdir=Path(spec["workdir"]), model=spec["model"], task_id=spec["task_id"],
                tracking=tracking, owns=tuple(spec["owns"]), session=history,
                receipt=Path(spec["receipt"]) if spec.get("receipt") else None,
                checkpoint=Path(spec["checkpoint"]), timeout_sec=timeout_sec,
                before_turn=inbox, on_progress=progress,
                max_turns=max_turns, effort=spec.get("effort", "high"),
                repo_context=spec.get("repo_context", ""), frozen_contracts=spec.get("frozen_contracts", ""),
                wrap_up_final_turn=is_readonly_stage,
            )
            result = harness.run(correction_note + spec["brief"])
        finally:
            stop_beat.set()
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
        if result.exit_code != 0 and result.final_text:
            print(result.final_text, file=sys.stderr, flush=True)
        return result.exit_code
    except sqlite3.Error as error:
        msg = f"internal store error: {error}"
        usage_path.write_text(json.dumps({"dispatch_id": spec["dispatch_id"], "exit_code": 3,
                                         "final_text": msg, "ended_at": now()}))
        print(msg, file=sys.stderr, flush=True)
        return 3
    finally:
        tracking.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
