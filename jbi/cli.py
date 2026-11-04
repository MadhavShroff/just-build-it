"""The `just-build-it` command.

    just-build-it                      start a new project and open the dashboard
    just-build-it --repo PATH          build new features in an existing repository
    just-build-it status [RUN_DIR]     print the state of a running dashboard as JSON
    just-build-it send TYPE [JSON]     send one protocol command to a running dashboard

The CLI starts the dashboard, prints verified links, and then shows the control menu.
The menu stops the run with one of four modes. The CLI is a protocol client like the
browser: it sends `run.stop` through the same command endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import __version__, netinfo, protocol
from .planner import slug
from .client import Client, ClientError, Control
from .engine import Engine
from .server import App, Auth, Server
from .store import Store, now

HOME = Path(os.environ.get("JBI_HOME", Path.home() / ".just-build-it"))
STOP_MENU = (
    ("1", "quit", "Quit now. Agents stop at once; unsaved work can be lost."),
    ("2", "standdown", "Stand down. Agents save work in progress to their branches, then stop."),
    ("3", "finish", "Finish assigned tasks, then stop. No new task starts."),
    ("4", "kill", "Kill. End this process and every process it started, immediately."),
)
BOLD, DIM, GREEN, CYAN, RESET = ("\033[1m", "\033[2m", "\033[32m", "\033[36m", "\033[0m") \
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else ("",) * 5


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="just-build-it", description="Build software end to end.")
    parser.add_argument("--version", action="version", version=f"just-build-it {__version__}")
    sub = parser.add_subparsers(dest="command")

    start = parser
    start.add_argument("--repo", type=Path, help="an existing git repository to add features to")
    start.add_argument("--name", default="new-app", help="the working name of a new project")
    start.add_argument("--port", type=int, default=0, help="the dashboard port (default: a free port)")
    start.add_argument("--max-sessions", type=int, default=3, help="agents that run at the same time")
    start.add_argument("--lanes", type=int, default=0, help="CI lanes (default: from the CPU count)")
    start.add_argument("--no-tailscale", action="store_true", help="listen on loopback only")
    start.add_argument("--offline", action="store_true", help="use the offline simulator for every model")
    start.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    start.add_argument("--no-menu", action="store_true", help="do not show the interactive control menu")

    status = sub.add_parser("status", help="print the state document of a running dashboard")
    status.add_argument("run_dir", nargs="?", type=Path)
    links = sub.add_parser("links", help="print fresh one-time sign-in links for a running dashboard")
    links.add_argument("run_dir", nargs="?", type=Path)
    send = sub.add_parser("send", help="send one protocol command to a running dashboard")
    send.add_argument("type")
    send.add_argument("payload", nargs="?", default="{}")
    send.add_argument("--run-dir", type=Path)
    return parser.parse_args(argv)


def default_lanes() -> int:
    return max(1, min(4, (os.cpu_count() or 4) // 4))


def make_completer(store: Store, *, offline: bool):
    """The planner's model access: the offline simulator, or the enabled provider accounts."""
    if offline:
        from .simulator import offline_completer
        return offline_completer()
    from .providers import DEBUG_LOG, completer
    return completer(lambda: store.snapshot()[1], debug_log=store.path.parent / DEBUG_LOG)


def latest_run_dir() -> Path:
    runs = sorted((HOME / "runs").glob("r-*"), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise SystemExit("No run found. Start one with: just-build-it")
    return runs[-1]


# ---- the run -------------------------------------------------------------------------

class Run:
    """One dashboard process: the store, the engine, the runtime and the listeners."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.run_id = "r-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = HOME / "runs" / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.stopped = threading.Event()
        self.stop_requested = threading.Event()     # the first SIGINT or SIGTERM sets it
        self.stop_mode: str | None = None

        mode = "existing" if args.repo else "new"
        target = str(args.repo.expanduser().resolve()) if args.repo else str(Path.home() / slug(args.name, "app"))
        if args.repo and not (Path(target) / ".git").exists():
            raise SystemExit(f"{target} is not a git repository.")
        name = Path(target).name if args.repo else slug(args.name, "app")
        tmux = f"tmux -L jbi-{self.run_id} attach"
        initial = protocol.initial_state(self.run_id, now(), name=name, mode=mode, target=target,
                                         max_sessions=args.max_sessions, lanes=args.lanes or default_lanes(),
                                         tmux=tmux)
        self.store = Store(self.dir / "run.db", initial)
        self._seed_accounts(args.offline)

        completer = make_completer(self.store, offline=args.offline)
        self.engine = Engine(self.store, completer)
        self.runtime = self._runtime()
        self.engine.runtime = self.runtime

        tailnet = None if args.no_tailscale else netinfo.detect_tailscale()
        self.auth = Auth()
        self.app = App(self.store, self.engine, self.auth,
                       extra_info=lambda: {"pid": os.getpid(), "run_dir": str(self.dir)},
                       link_bases=lambda: [{"label": l.label, "url": l.url} for l in self.links if l.verified])
        self.server = Server(self.app, netinfo.hosts(tailnet), args.port).start()
        self.links = netinfo.links(self.server.port, self.run_id, tailnet)
        Control(self.links[0].url, self.auth.token, self.run_id, os.getpid()).save(self.dir / "control.json")

    def _seed_accounts(self, offline: bool) -> None:
        from . import providers
        from .store import Changes
        changes = Changes()
        for name in providers.catalog()["providers"]:
            account = providers.blank_account(name)
            account["enabled"] = name == "simulator" if offline else name in ("claude", "simulator")
            changes.set("accounts", name, account)
        self.store.commit("account.status", changes)

    def _runtime(self):
        from .supervisor import Supervisor
        # The supervisor reads the repository from state.run.target, which the user sets in
        # the stack step. Offline runs enable only the simulator account, so selection picks it.
        return Supervisor(self.store, RunHooks(self), self.dir, parent_pid=os.getpid())

    def link_lines(self) -> list[str]:
        lines = []
        for link in self.links:
            if link.verified:
                key = self.auth.new_key()
                lines.append(f"  {BOLD}{link.label:<10}{RESET}{CYAN}{link.url}/#k={key}{RESET}")
            else:
                lines.append(f"  {DIM}{link.label:<10}not available: {link.note}{RESET}")
        return lines

    def close(self) -> None:
        self.server.stop()
        self.store.close()


class RunHooks:
    """`jbi.contracts.Hooks` for a CLI run: forward to the engine; `stopped` also ends the run."""

    def __init__(self, run: Run):
        self._run = run

    def bootstrap_finished(self, ok: bool, detail: str, commit: str | None) -> None:
        self._run.engine.bootstrap_finished(ok, detail, commit)

    def task_merged(self, task: str, commit: str, architecture_patch: dict | None) -> None:
        self._run.engine.task_merged(task, commit, architecture_patch)

    def wave_completed(self, wave: int) -> None:
        self._run.engine.wave_completed(wave)

    def all_merged(self) -> None:
        self._run.engine.all_merged()

    def stopped(self, mode: str) -> None:
        self._run.engine.stopped(mode)
        self._run.stop_mode = mode
        self._run.stopped.set()


def banner(run: Run) -> None:
    print(f"\n{BOLD}just-build-it{RESET} {DIM}{__version__} · run {run.run_id}{RESET}\n")
    print("Open the dashboard. Setup continues in the browser.\n")
    for line in run.link_lines():
        print(line)
    print(f"\n{DIM}Each link signs in once. Press n for new links.")
    print(f"Agents run in tmux. Watch them with: {run.store.read('/run/tmux')}{RESET}\n")


def menu(run: Run) -> None:
    client = Client.from_control(Control.load(run.dir / "control.json"))
    print(f"{BOLD}Stop options{RESET}")
    for key, _, text in STOP_MENU:
        print(f"  {BOLD}{key}{RESET}  {text}")
    print(f"  {BOLD}n{RESET}  Print new sign-in links\n")
    while not run.stopped.is_set():
        try:
            choice = input("> ").strip().lower()
        except EOFError:
            run.stopped.wait()
            return
        if choice == "n":
            for line in run.link_lines():
                print(line)
            continue
        mode = next((m for k, m, _ in STOP_MENU if choice in (k, m)), None)
        if mode is None:
            print("Type 1, 2, 3, 4 or n.")
            continue
        if mode in ("quit", "kill") and input(f"{mode} the run? Type yes: ").strip().lower() != "yes":
            continue
        if mode == "kill":
            hard_kill(run)
        try:
            client.send("run.stop", {"mode": mode})
            print(f"{GREEN}{mode}{RESET} started. The dashboard shows progress.")
        except ClientError as error:
            print(f"Refused: {error.message}")


def hard_kill(run: Run) -> None:
    """SIGKILL every process the run started, then this process. Needs no server.

    `runtime.stop("kill")` returns only after it has signalled every agent group. Then the
    private tmux server goes, and last this process gets SIGKILL. This function never
    signals the CLI's own process group, which the user's shell can share.
    """
    try:
        run.runtime.stop("kill")
    finally:
        subprocess.run(["tmux", "-L", f"jbi-{run.run_id}", "kill-server"], capture_output=True, check=False)
        os.kill(os.getpid(), signal.SIGKILL)


def signal_handler(run: Run, kill=hard_kill):
    """The SIGINT and SIGTERM handler. The first signal stands down; any later signal kills."""
    def on_signal(signum, frame) -> None:
        if run.stop_requested.is_set() or run.stopped.is_set():
            print("\nKilling the run now.", flush=True)
            kill(run)
            return
        run.stop_requested.set()
        print("\nStanding down. Press Ctrl-C again to kill.", flush=True)
        threading.Thread(target=lambda: run.engine.submit(
            {"id": f"sig-{time.time_ns()}", "type": "run.stop", "payload": {"mode": "standdown"}}),
            name="jbi-standdown", daemon=True).start()
    return on_signal


def serve(args: argparse.Namespace) -> int:
    run = Run(args)
    banner(run)
    if args.open:
        import webbrowser
        link = next((l for l in run.links if l.verified), None)
        if link:
            webbrowser.open(f"{link.url}/#k={run.auth.new_key()}")

    on_signal = signal_handler(run)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    if sys.stdin.isatty() and not args.no_menu:
        threading.Thread(target=menu, args=(run,), name="jbi-menu", daemon=True).start()
    run.stopped.wait()
    print(f"Run stopped ({run.stop_mode}). The record stays in {run.dir}")
    run.close()
    return 0


def run_client(run_dir: Path | None) -> Client:
    """A protocol client for a running dashboard, or a clear exit when there is none."""
    path = (run_dir or latest_run_dir()) / "control.json"
    try:
        return Client.from_control(Control.load(path))
    except (OSError, ValueError, KeyError):
        raise SystemExit(f"No running dashboard at {path.parent}. Start one with: just-build-it")


def client_command(args: argparse.Namespace) -> int:
    """`status`, `links` and `send`: one protocol call; machine-readable errors on stderr."""
    client = run_client(getattr(args, "run_dir", None))
    try:
        if args.command == "status":
            print(json.dumps(client.state(), indent=2))
        elif args.command == "links":
            for link in client.links():
                print(f"{link['label']:<10}{link['url']}")
        else:
            try:
                payload = json.loads(args.payload)
            except json.JSONDecodeError as error:
                raise ClientError(400, "payload_invalid", f"The payload is not valid JSON: {error.msg}.")
            print(json.dumps(client.send(args.type, payload)))
    except ClientError as error:
        print(json.dumps({"error": {"code": error.code, "message": error.message}}), file=sys.stderr)
        return 1
    except OSError:
        print(json.dumps({"error": {"code": "unreachable", "message": "The dashboard does not answer. "
                                    "The run has stopped."}}), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command in ("status", "links", "send"):
        return client_command(args)
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
