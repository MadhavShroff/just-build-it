"""Internal contracts between the engine and the execution runtime.

The engine owns the phase machine, the plan, the stack, the accounts and the
architecture graph. The runtime owns sessions, lanes and task progress. The two
meet only through these two protocols, so either side can be replaced or faked.

State ownership (who may commit changes under which top-level key):

    engine   run, config, accounts, intake, plan (structure), stack, bootstrap.status
             transitions, architecture, decisions it opens itself
    runtime  sessions, lanes, telemetry, tasks/<id>/{status, stage, stages, attempts,
             attempt_history, resume_after, blocked_reason, commit, receipt, findings},
             bootstrap.detail, bootstrap.commit and bootstrap.branch, decisions it opens

Both sides write through `Store.commit` or `Store.log`, never by mutating state.
"""

from __future__ import annotations

from typing import Protocol


class Hooks(Protocol):
    """Callbacks from the runtime to the engine. Each call is fast and thread-safe."""

    def bootstrap_finished(self, ok: bool, detail: str, commit: str | None) -> None:
        """Bootstrap ended. On success the engine moves the run to `configure`."""

    def task_merged(self, task: str, commit: str, architecture_patch: dict | None) -> None:
        """A task's lane merged it. The engine runs the architecture.sync hook."""

    def wave_completed(self, wave: int) -> None:
        """Every task in a wave is merged."""

    def all_merged(self) -> None:
        """Every task is merged. The engine moves the run to `complete`."""

    def stopped(self, mode: str) -> None:
        """The runtime finished a stop mode. The CLI process may exit."""


class Runtime(Protocol):
    """The execution side: tmux sessions, the scheduler, CI lanes, the process tree."""

    def start_bootstrap(self) -> None:
        """Start the bootstrap agent for `state.run.target` and `state.stack`."""

    def start_execution(self) -> None:
        """Start the scheduler. It admits every ready task, up to `config.max_sessions`."""

    def wake(self) -> None:
        """State that affects scheduling changed (config, override, answer). Rescan now."""

    def control(self, task: str, action: str) -> None:
        """Apply `retry`, `pause` or `resume` to one task."""

    def answer(self, decision: str, answer: str) -> None:
        """A decision that the runtime opened has an answer."""

    def retry_bootstrap(self) -> None:
        """Run bootstrap again in the same target after a failure."""

    def stop(self, mode: str) -> None:
        """Apply a stop mode: quit, standdown, finish or kill. Returns at once, except for
        kill, which sends every SIGKILL before it returns. The runtime calls
        `Hooks.stopped(mode)` when the mode completes."""
