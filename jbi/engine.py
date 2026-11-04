"""The engine: the phase machine and the command handlers.

`Engine.submit` is the only write path for clients. It validates a command, checks the
phase, de-duplicates by command id, and calls one handler. A handler commits events
and returns at once. Model calls run on a worker thread and show progress in
`run.busy`, so the HTTP request never waits for a model.
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from . import architecture, planner, providers, protocol, stack, waves
from .contracts import Runtime
from .llm import Completer, CompletionError
from .protocol import ProtocolError
from .store import Changes, Store, now

Handler = Callable[[str, dict], None]          # (command type, validated payload)


class Engine:
    def __init__(self, store: Store, completer: Completer, runtime: Runtime | None = None):
        self.store, self.completer, self.runtime = store, completer, runtime
        self._work_lock = threading.Lock()
        # One lock around lookup, phase check, handler and record, so a repeated id runs once.
        self._lock = threading.RLock()
        self.debug_log = Path(store.path).parent / providers.DEBUG_LOG
        self._handlers: dict[str, Handler] = {
            "intake.import": self._intake_import, "intake.say": self._intake_say,
            "intake.detail": self._intake_detail, "intake.finish": self._intake_finish,
            "stack.revise": self._stack_revise, "stack.accept": self._stack_accept,
            "stack.back": self._stack_back, "bootstrap.retry": self._bootstrap_retry,
            "run.configure": self._run_configure,
            "run.start": self._run_start, "task.override": self._task_override,
            "task.retry": self._task_control, "task.pause": self._task_control,
            "task.resume": self._task_control, "decision.answer": self._decision_answer,
            "account.set": self._account_set, "account.probe": self._account_probe,
            "architecture.refresh": self._architecture_refresh, "run.stop": self._run_stop,
        }
        missing = set(protocol.COMMANDS) - set(self._handlers)
        assert not missing, f"commands without a handler: {missing}"

    # ---- the write path --------------------------------------------------------------

    def submit(self, body: Any) -> dict:
        cid, ctype, payload = protocol.validate_command(body)
        with self._lock:
            previous = self.store.command_result(cid)
            if previous is not None:
                return previous
            phase = self.store.read("/run/phase")
            if phase not in protocol.COMMANDS[ctype].phases:
                raise ProtocolError("phase_invalid",
                                    f"{ctype} needs phase {' or '.join(protocol.COMMANDS[ctype].phases)}; "
                                    f"the run is in phase '{phase}'.",
                                    expected=list(protocol.COMMANDS[ctype].phases), actual=phase)
            self._handlers[ctype](ctype, payload)
            result = {"protocol": protocol.PROTOCOL_VERSION, "accepted": True, "id": cid, "seq": self.store.seq}
            self.store.record_command(cid, ctype, protocol.redact(ctype, payload), result)
            return result

    # ---- helpers ---------------------------------------------------------------------

    def _phase(self, phase: str, text: str) -> None:
        self.store.log("phase", text, extra=Changes().replace("run", "phase", phase),
                       etype="run.phase", phase=phase)

    def _background(self, label: str, work: Callable[[], None]) -> None:
        """Run model work on one worker thread. One job at a time; the UI shows `run.busy`."""
        if not self._work_lock.acquire(blocking=False):
            raise ProtocolError("conflict", "The planner is busy. Wait for the current step to finish.",
                                busy=self.store.read("/run/busy"))
        self.store.commit("run.config", Changes().replace("run", "busy", {"label": label, "since": now()}))

        def body() -> None:
            try:
                work()
            except Exception as error:  # a model or parse failure must reach the user, not vanish
                # The timeline gets a fixed sentence only. A CompletionError message is built
                # from fixed sentences in `providers`; other text stays in the 0600 debug log.
                providers.write_debug(self.debug_log, f"{label} failed", traceback.format_exc())
                text = (f"{label} failed: {error}" if isinstance(error, CompletionError)
                        else f"{label} failed ({type(error).__name__}). The details are in the run's debug log.")
                self.store.log("error", text, kind_detail=type(error).__name__)
            finally:
                self.store.commit("run.config", Changes().replace("run", "busy", None))
                self._work_lock.release()

        threading.Thread(target=body, name=f"jbi-{label}", daemon=True).start()

    def _say(self, role: str, text: str, **extra: Any) -> None:
        message = {"role": role, "text": text, "ts": now(), **extra}
        self.store.commit("chat.message", Changes().append("intake", "messages", message), {"role": role})

    def _require_runtime(self) -> Runtime:
        if self.runtime is None:
            raise ProtocolError("conflict", "No execution runtime is attached to this run.")
        return self.runtime

    # ---- intake ----------------------------------------------------------------------

    def _intake_say(self, ctype: str, payload: dict) -> None:
        self._say("user", payload["text"])

        def reply() -> None:
            state = self.store.snapshot()[1]
            answer = planner.chat_reply(self.completer, state)
            self._say("assistant", answer.text, ready=answer.ready)
        self._background("Thinking", reply)

    def _intake_detail(self, ctype: str, payload: dict) -> None:
        self.store.log("intake", f"Detail level set to {payload['level']}",
                       extra=Changes().replace("intake", "detail", payload["level"]), etype="run.config")

    def _intake_import(self, ctype: str, payload: dict) -> None:
        self._say("user", payload["text"], imported=True)

        def convert() -> None:
            draft = planner.from_import(self.completer, payload["text"])
            self.store.log("plan", "Imported list converted to a rough PRD",
                           extra=Changes().replace("plan", "rough_prd", draft.rough_prd), etype="plan.updated")
            self._say("assistant", draft.summary, ready=True)
        self._background("Reading your list", convert)

    def _intake_finish(self, ctype: str, payload: dict) -> None:
        if not self.store.read("/intake/messages"):
            raise ProtocolError("conflict", "Describe the project or import a list before you stop questions.")

        def finish() -> None:
            state = self.store.snapshot()[1]
            result = planner.finish(self.completer, state)
            plan = waves.compile_plan(result.tasks, self.store.read("/plan/revision", 0) + 1)
            plan.update(rough_prd=result.rough_prd, prd=result.prd)
            graph = architecture.initial(result.prd, plan)
            proposal = stack.propose(self.completer, result.prd, state["intake"]["messages"])
            name = result.prd.get("name") or self.store.read("/run/name")
            changes = (Changes().replace("plan", plan).replace("architecture", graph)
                       .replace("run", "name", name))
            self.store.log("plan", f"Plan ready: {len(plan['order'])} tasks in {len(plan['waves'])} waves",
                           extra=changes, etype="plan.updated", revision=plan["revision"])
            self._propose(proposal)
            self._phase("stack", "Stack proposal ready")
        self._background("Writing the plan", finish)

    # ---- stack -----------------------------------------------------------------------

    def _propose(self, proposal: stack.Proposal) -> None:
        self.store.log("stack", f"Proposed tech stack: {proposal.summary}",
                       extra=Changes().replace("stack", proposal.as_state()), etype="stack.proposed")

    def _stack_revise(self, ctype: str, payload: dict) -> None:
        def revise() -> None:
            state = self.store.snapshot()[1]
            proposal = stack.propose(self.completer, state["plan"]["prd"], state["intake"]["messages"],
                                     request=payload["request"], current=state["stack"])
            self._propose(proposal)
        self._background("Revising the stack", revise)

    def _stack_back(self, ctype: str, payload: dict) -> None:
        if self.store.read("/run/phase") == "bootstrap":
            # Only a failed bootstrap may go back. The user then edits the stack or the target.
            if self.store.read("/bootstrap/status") != "failed":
                raise ProtocolError("conflict", "stack.back from bootstrap needs a failed bootstrap.",
                                    status=self.store.read("/bootstrap/status"))
            changes = Changes().replace("stack", "accepted", False).replace("bootstrap", "status", "pending")
            self.store.log("stack", "Back to the stack proposal after a failed bootstrap", extra=changes,
                           etype="bootstrap.status")
            self._phase("stack", "Back to the stack proposal")
            return
        self._phase("intake", "Back to planning questions")

    def _stack_accept(self, ctype: str, payload: dict) -> None:
        mode = self.store.read("/run/mode")
        target = Path(payload["target"]).expanduser()
        if not target.is_absolute():
            raise ProtocolError("payload_invalid", "The target must be an absolute path or start with ~.",
                                field="target")
        if mode == "new" and target.exists() and any(target.iterdir()):
            raise ProtocolError("conflict", f"{target} is not empty. Choose an empty or new folder.",
                                field="target")
        if mode == "existing" and not (target / ".git").exists():
            raise ProtocolError("conflict", f"{target} is not a git repository.", field="target")
        changes = (Changes().replace("stack", "accepted", True).replace("run", "target", str(target))
                   .replace("bootstrap", "status", "running"))
        if payload.get("name"):
            changes.replace("run", "name", payload["name"])
        self.store.log("stack", f"Stack accepted. Bootstrapping {target}", extra=changes, etype="bootstrap.status")
        self._phase("bootstrap", "Bootstrap started")
        self._require_runtime().start_bootstrap()

    def _bootstrap_retry(self, ctype: str, payload: dict) -> None:
        if self.store.read("/bootstrap/status") != "failed":
            raise ProtocolError("conflict", "bootstrap.retry needs a failed bootstrap.",
                                status=self.store.read("/bootstrap/status"))
        runtime = self._require_runtime()
        if not callable(getattr(runtime, "retry_bootstrap", None)):
            raise ProtocolError("conflict", "This runtime cannot retry bootstrap. Use stack.back instead.")
        self.store.log("bootstrap", "Bootstrap retry started",
                       extra=Changes().replace("bootstrap", "status", "running"), etype="bootstrap.status")
        runtime.retry_bootstrap()

    # ---- configure and execute -------------------------------------------------------

    def _run_configure(self, ctype: str, payload: dict) -> None:
        changes = Changes().replace("config", "rigor", payload["rigor"]).replace("config", "profile",
                                                                                   payload["profile"])
        self.store.log("config", f"Rigor {payload['rigor']}, models {payload['profile']}",
                       extra=changes, etype="run.config")
        if self.runtime and self.store.read("/run/phase") == "execute":
            self.runtime.wake()

    def _run_start(self, ctype: str, payload: dict) -> None:
        self._phase("execute", "Build started")
        self._require_runtime().start_execution()

    def _task(self, tid: str) -> dict:
        task = self.store.read(f"/plan/tasks/{tid}")
        if task is None:
            raise ProtocolError("not_found", f"No task {tid!r} in the plan.", task=tid)
        return task

    def _task_override(self, ctype: str, payload: dict) -> None:
        self._task(payload["task"])
        override = {k: payload[k] for k in ("rigor", "profile", "model") if payload.get(k)}
        text = ", ".join(f"{k} {v}" for k, v in override.items()) or "defaults"
        self.store.log("config", f"{payload['task']}: {text}",
                       extra=Changes().replace("plan", "tasks", payload["task"], "override", override),
                       etype="task.override", task=payload["task"])
        if self.runtime and self.store.read("/run/phase") == "execute":
            self.runtime.wake()

    def _task_control(self, ctype: str, payload: dict) -> None:
        self._task(payload["task"])
        self._require_runtime().control(payload["task"], ctype.split(".", 1)[1])

    def _decision_answer(self, ctype: str, payload: dict) -> None:
        decision = self.store.read(f"/decisions/{payload['decision']}")
        if decision is None or decision.get("status") != "open":
            raise ProtocolError("conflict", "That decision is not open.", decision=payload["decision"])
        changes = (Changes().replace("decisions", payload["decision"], "status", "answered")
                   .set("decisions", payload["decision"], "answer", payload["answer"]))
        self.store.log("decision", f"Answered: {decision['title']}", extra=changes,
                       etype="decision.answered", decision=payload["decision"])
        if decision.get("owner") == "runtime" and self.runtime:
            self.runtime.answer(payload["decision"], payload["answer"])

    # ---- accounts ----------------------------------------------------------------------

    def _account_set(self, ctype: str, payload: dict) -> None:
        name = payload["provider"]
        if name not in providers.catalog()["providers"]:
            raise ProtocolError("payload_invalid", f"Unknown provider {name!r}.", field="provider")
        key_note = providers.store_key(name, payload["api_key"]) if payload.get("api_key") else None
        account = self.store.read(f"/accounts/{name}") or providers.blank_account(name)
        account["enabled"] = payload["enabled"]
        if key_note:
            account["key"] = key_note
        self.store.log("account", f"{account['label']} {'enabled' if account['enabled'] else 'disabled'}",
                       extra=Changes().set("accounts", name, account), etype="account.status", provider=name)

    def _account_probe(self, ctype: str, payload: dict) -> None:
        name = payload["provider"]
        if name not in providers.catalog()["providers"]:
            raise ProtocolError("payload_invalid", f"Unknown provider {name!r}.", field="provider")

        def probe() -> None:
            result = providers.probe(name)
            account = self.store.read(f"/accounts/{name}") or providers.blank_account(name)
            account.update(status=result.status, detail=result.detail, checked=now(), models=result.models)
            self.store.log("account", f"{account['label']}: {result.status}",
                           extra=Changes().set("accounts", name, account), etype="account.status", provider=name)
        threading.Thread(target=probe, name=f"jbi-probe-{name}", daemon=True).start()

    # ---- architecture ------------------------------------------------------------------

    def _architecture_refresh(self, ctype: str, payload: dict) -> None:
        self.sync_architecture("refresh", None, None)

    def sync_architecture(self, event: str, task: str | None, patch: dict | None) -> None:
        """The architecture.sync hook. Applies a structural patch only when one is valid."""
        with self.store.lock:
            graph, plan = self.store.read("/architecture"), self.store.read("/plan")
            result = architecture.sync(graph, plan, event=event, task=task, patch=patch)
            if result.changed:
                self.store.log("architecture", result.summary,
                               extra=Changes().replace("architecture", result.graph),
                               etype="architecture.updated", trigger=event)

    # ---- hooks from the runtime --------------------------------------------------------

    def bootstrap_finished(self, ok: bool, detail: str, commit: str | None) -> None:
        changes = Changes().replace("bootstrap", "status", "passed" if ok else "failed")
        self.store.log("bootstrap", "Bootstrap passed" if ok else f"Bootstrap failed: {detail}",
                       extra=changes, etype="bootstrap.status")
        if ok:
            self.sync_architecture("bootstrap.completed", None, None)
            self._phase("configure", "Choose rigor and models")

    def task_merged(self, task: str, commit: str, architecture_patch: dict | None) -> None:
        self.sync_architecture("task.merged", task, architecture_patch)

    def wave_completed(self, wave: int) -> None:
        self.sync_architecture("wave.completed", None, None)
        self.store.log("wave", f"Wave {wave} complete", wave=wave)

    def all_merged(self) -> None:
        self._phase("complete", "Every task is merged")

    def stopped(self, mode: str) -> None:
        self.store.log("run", f"Stop mode {mode} complete", etype="run.stopping", mode=mode)

    def _run_stop(self, ctype: str, payload: dict) -> None:
        mode = payload["mode"]
        self.store.log("run", f"Stop requested: {mode}",
                       extra=Changes().replace("run", "stopping", {"mode": mode, "since": now()}),
                       etype="run.stopping", mode=mode)
        if self.runtime:
            self.runtime.stop(mode)
        else:
            self.stopped(mode)
