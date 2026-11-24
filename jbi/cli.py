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
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, metrics, netinfo, protocol, worktrees as wt
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
    start.add_argument("--dag", type=Path, help="a typed task DAG file (.json or restricted YAML)")
    start.add_argument("--start", action="store_true", help="start execution immediately after loading the DAG")
    start.add_argument("--name", default="new-app", help="the working name of a new project")
    start.add_argument("--port", type=int, default=0, help="the dashboard port (default: a free port)")
    start.add_argument("--max-sessions", type=int, default=3, help="agents that run at the same time")
    start.add_argument("--lanes", type=int, default=0, help="legacy parallelism field; CI owns its own queue")
    start.add_argument("--base", default=None,
                       help="the branch every task branch is cut from. Default: main")
    start.add_argument("--branch", default=None,
                       help="the branch where verified work lands. Default: the --base branch")
    start.add_argument("--cascade", default=None,
                       help="'founder', or a JSON list of {provider, model} entries in failover "
                            "order. The founder cascade is claude-opus-5-5, then Gemini Neon, "
                            "then Carbon. A morph model is refused.")
    start.add_argument("--ci-service", choices=("ci-conductor",), default="ci-conductor",
                       help="the only service that can merge a verified task")
    start.add_argument("--no-tailscale", action="store_true", help="listen on loopback only")
    start.add_argument("--provider", default="agy", choices=("agy", "openai-compatible", "claude", "codex"),
                       help="the provider that dispatches agent stages. Default agy: the standing order of "
                            "2026-11-13 puts no Claude model in any subagent. Name claude only when the "
                            "founder asks for it for this run.")
    start.add_argument("--offline", action="store_true", help="use the offline simulator for every model")
    start.add_argument("--dry-run", action="store_true",
                       help="rehearse the whole pipeline without touching the repo, the CI service or "
                            "the train: every stage runs against the simulator and the landing path is "
                            "reported, never taken")
    start.add_argument("--preview", action="store_true",
                       help="serve the dry-run DAG preview UI showing waves, parallel tasks, "
                            "critical path and live ETAs")
    start.add_argument("--base-path", default="",
                       help="mount the dashboard under a URL prefix, such as /jbi/preview, when a "
                            "reverse proxy forwards it. The prefix is stripped before routing, so "
                            "the page's relative links keep working.")
    start.add_argument("--publish-preview", action="store_true",
                       help="answer the read-only preview and health routes without a browser "
                            "session, so a proxy that holds its own authentication can forward "
                            "them. Every other route still needs a session or the machine token.")
    start.add_argument("--registry", type=Path, default=None,
                       help="write a small JSON record of this run (port, base path, DAG, run id) "
                            "so another service can find the preview. Defaults to "
                            "~/.just-build-it/preview.json for a preview run.")
    start.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    start.add_argument("--no-menu", action="store_true", help="do not show the interactive control menu")

    status = sub.add_parser("status", help="print the state document of a running dashboard")
    status.add_argument("run_dir", nargs="?", type=Path)
    links = sub.add_parser("links", help="print fresh one-time sign-in links for a running dashboard")
    links.add_argument("run_dir", nargs="?", type=Path)
    fleet = sub.add_parser("fleet", help="print internal sessions, tasks, leases, CI, messages, and costs")
    fleet.add_argument("--run-dir", type=Path)
    fleet.add_argument("--offline", action="store_true", help="read the SQLite record directly")
    send = sub.add_parser("send", help="send one protocol command or agent message")
    send.add_argument("type", nargs="?", help="protocol command type")
    send.add_argument("payload", nargs="?", default="{}")
    send.add_argument("--run-dir", type=Path)
    send.add_argument("--agent", help="target an agent session; use 'all' for every session")
    send.add_argument("--all", action="store_true", help="send to every session")
    send.add_argument("--text", help="message text for --agent or --all")
    resume = sub.add_parser("resume", help="reopen a stored run and continue from stored task states")
    resume.add_argument("run_dir", nargs="?", type=Path)
    resume.add_argument("--port", type=int, default=0, help="the dashboard port (default: a free port)")
    resume.add_argument("--ci-service", choices=("ci-conductor",), default=None,
                        help="the only service that can merge a verified task")
    resume.add_argument("--no-tailscale", action="store_true", help="listen on loopback only")
    resume.add_argument("--offline", action="store_true", help="use the offline simulator for every model")
    resume.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    resume.add_argument("--no-menu", action="store_true", help="do not show the interactive control menu")
    resume.add_argument("--if-exists", action="store_true", help="exit 0 cleanly when no stored run exists")
    budget = sub.add_parser("budget", help="set or show one CPU and memory budget for a whole process tree")
    budget_sub = budget.add_subparsers(dest="budget_command")
    budget_set = budget_sub.add_parser("set", help="set the budget on a process and its whole tree")
    budget_set.add_argument("pid", type=int)
    budget_set.add_argument("--cpus", type=float, required=True)
    budget_set.add_argument("--mem-gib", type=float, required=True)
    budget_mode = budget_set.add_mutually_exclusive_group()
    budget_mode.add_argument("--graceful", dest="graceful", action="store_true",
                             help="lower the ceiling and let work finish (default)")
    budget_mode.add_argument("--force", dest="force", action="store_true",
                             help="apply now and kill the tree's own processes until it fits")
    budget_set.add_argument("--deadline-s", dest="deadline_s", type=float, default=30.0,
                            help="the graceful deadline in seconds (default: 30)")
    budget_show = budget_sub.add_parser("show", help="print the kernel account of a tree")
    budget_show.add_argument("pid", type=int)
    return parser.parse_args(argv)


def default_lanes() -> int:
    return max(1, min(4, (os.cpu_count() or 4) // 4))


def apply_branch_flags(store: Store, repo: Path | None, base: str | None,
                       branch: str | None, *, dry_run: bool = False) -> None:
    """Validate --base and --branch at start, then store them for the runtime.

    A refusal happens here, before any agent runs. A --base that names no branch is a
    named refusal: task branches would cut from nothing. A commit SHA as --base or as
    --branch is refused, with the flag named, because `fast_forward_main` needs a
    branch to advance. A --branch that names a tag or another non-branch ref is
    refused for the same reason. A --branch that names no branch is created from
    --base, and the start log says so. Under --dry-run the repository is never
    touched: a missing --branch is a `branch_missing` refusal instead. An empty
    --branch means the landing branch is the --base branch.
    """
    from .store import Changes
    base = (base or "").strip()
    branch = (branch or "").strip()
    if not base and not branch:
        return
    for flag, name in (("--base", base), ("--branch", branch)):
        if name and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", name):
            raise SystemExit(f"branch_refused: {flag} must name a branch, not a commit: "
                             "fast_forward_main needs a branch to advance")
    base = base or wt.MAIN
    landing = branch or base
    if repo is not None:
        if wt.missing_branch(repo, base):
            raise SystemExit(f"base_missing: base branch {base!r} does not exist in {repo}; "
                             "every task branch would cut from nothing")
        if wt.missing_branch(repo, landing):
            if wt.head(repo, landing) is not None:
                raise SystemExit(f"branch_refused: --branch {landing!r} names a tag or another "
                                 "non-branch ref, not a branch: fast_forward_main needs a "
                                 "branch to advance")
            if dry_run:
                raise SystemExit(f"branch_missing: --branch {landing!r} does not exist in {repo}; "
                                 "a dry run never creates a branch")
            wt.ensure_branch(repo, landing, base)
            store.log("run", f"created branch {landing} from base {base}", etype="run.config")
    store.commit("run.config", Changes().set("bootstrap", "base_branch", base)
                 .set("bootstrap", "branch", landing), {})


def cascade_providers(cascade: str | None) -> set[str]:
    """Every distinct provider an explicit --cascade names. 'founder' names claude and agy.

    A JSON cascade names its providers directly, and each one needs an enabled
    account, or `models.select` silently falls back to the default provider.
    Invalid JSON names nothing; `install_cascade` refuses the run for it later.
    """
    if cascade == "founder":
        from . import models
        return {str(entry["provider"]).strip().lower() for entry in models.FOUNDER_CASCADE}
    if not cascade:
        return set()
    try:
        entries = json.loads(cascade)
    except json.JSONDecodeError:
        return set()
    if not isinstance(entries, list):
        return set()
    return {str(entry.get("provider")).strip().lower() for entry in entries
            if isinstance(entry, dict) and str(entry.get("provider") or "").strip()}


def _enable_cascade_accounts(store: Store, entries: list[dict], *, offline: bool = False) -> None:
    """Enable every catalog provider named by `entries`, unless the run is offline or dry-run."""
    if offline or store.read("/config/dry_run"):
        return
    from . import providers
    from .store import Changes
    known = providers.catalog()["providers"]
    changes = Changes()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("provider") or "").strip().lower()
        if name not in known:
            continue
        account = store.read(f"/accounts/{name}") or providers.blank_account(name)
        if not account.get("enabled"):
            account = {**account, "enabled": True}
            changes.set("accounts", name, account)
    if changes:
        store.commit("account.status", changes, {})


def install_cascade(store: Store, cascade: str | None, *, offline: bool = False) -> None:
    """Store the run's cascade override in `provider_cascade`. 'founder' installs the founder order.

    The cascade is an ordered list of {provider, model} entries. `models.select` walks
    it before the account default. A morph model refuses at start: morph answers 401,
    and nothing in jbi may depend on it.
    """
    from . import models
    from .store import Changes
    if cascade == "founder":
        entries = [dict(entry) for entry in models.FOUNDER_CASCADE]
    else:
        try:
            entries = json.loads(cascade or "")
        except json.JSONDecodeError as error:
            raise SystemExit(f"cascade_invalid: --cascade is 'founder' or a JSON list of "
                             f"{{provider, model}} entries: {error.msg}")
        if not isinstance(entries, list) or not entries:
            raise SystemExit("cascade_invalid: --cascade needs 'founder' or a non-empty JSON list")
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("provider"):
            raise SystemExit("cascade_invalid: every cascade entry names a provider")
        name = f"{entry.get('provider')}/{entry.get('model')}"
        if ("morph" in str(entry.get("model") or "").casefold()
                or "morph" in str(entry.get("provider") or "").casefold()):
            raise SystemExit(f"morph_refused: cascade entry {name!r} is a morph "
                             "model; morph answers 401 and nothing in jbi may depend on it")
    store.commit("config", Changes().set("config", "provider_cascade", entries), {})
    _enable_cascade_accounts(store, entries, offline=offline)
    order = ", ".join(f"{entry.get('provider')}/{entry.get('model')}" for entry in entries)
    store.log("config", f"Cascade installed: {order}", etype="run.config")


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


def prepare_resume(store: Store, *, offline: bool = False) -> list[str]:
    """Reconcile stored task states so an interrupted run continues cleanly on reopen."""
    from . import resume as resume_mod
    from . import rigor
    from .store import Changes

    _reclaim_orphans_on_resume(store, resume_mod)
    cascade = store.read("/config/provider_cascade")
    if isinstance(cascade, list) and cascade:
        _enable_cascade_accounts(store, cascade, offline=offline)

    changes = Changes()
    if store.read("/run/stopping") is not None:
        changes.replace("run", "stopping", None)
    if store.read("/run/busy") is not None:
        changes.replace("run", "busy", None)

    plan = store.read("/plan") or {}
    config = store.read("/config") or {"rigor": "high", "profile": "balanced"}
    tasks = plan.get("tasks") or {}
    resumed_tasks: list[str] = []

    for tid in plan.get("order") or list(tasks):
        task = tasks.get(tid)
        if not isinstance(task, dict):
            continue
        status = task.get("status")
        if status in ("running", "verifying", "queued", "merging", "paused"):
            resume_after = task.get("resume_after")
            if resume_after is None:
                level, _ = rigor.effective(task, config)
                stages = task.get("stages") or {}
                last_passed: str | None = None
                for stage in rigor.pipeline(level):
                    if stage.role == "merge":
                        break
                    if (stages.get(stage.name) or {}).get("status") == "passed":
                        last_passed = stage.name
                    else:
                        break
                resume_after = last_passed
            changes.set("plan", "tasks", tid, "status", "ready")
            changes.set("plan", "tasks", tid, "resume_after", resume_after)
            resumed_tasks.append(tid)
        elif status == "blocked":
            deps = task.get("deps") or []
            if deps and all((tasks.get(d) or {}).get("status") == "merged" for d in deps):
                changes.set("plan", "tasks", tid, "status", "ready")
                resumed_tasks.append(tid)

    store.log("run", f"Run resumed ({len(resumed_tasks)} task(s) ready)",
              extra=changes, etype="run.config")
    return resumed_tasks


def _reclaim_orphans_on_resume(store: Store, resume_mod) -> None:
    """Reclaim orphans of a previous run before re-opening (REQUIREMENT-resume 2).

    Reads the snapshot once: the run id, the target repo, and any GATE job ids
    the run recorded. Records the report at `run_dir / "resume-record.json"`. A
    failure here is logged and never aborts the resume (2.5).
    """
    _revision, state = store.snapshot()
    run_section = (state or {}).get("run") or {}
    run_id = run_section.get("id")
    target = run_section.get("target")
    gate_ids = (state or {}).get("gate_jobs") or []
    run_dir = Path(store.path).parent
    try:
        report = resume_mod.orphaned(
            str(run_id) if run_id else run_dir.name,
            run_dir,
            target_repo=Path(target) if target else None,
            gate_ids=list(gate_ids) if isinstance(gate_ids, list) else [],
        )
        resume_mod.write_resume_record(run_dir, report)
    except Exception as error:                          # never abort the resume (2.5)
        try:
            (run_dir / "resume-record.json").write_text(
                json.dumps({"error": f"{type(error).__name__}: {error}"}) + "\n")
        except OSError:
            pass


# ---- the run -------------------------------------------------------------------------

class Run:
    """One dashboard process: the store, the engine, the runtime and the listeners."""

    def __init__(self, args: argparse.Namespace, *, resume_dir: Path | None = None):
        self.args = args
        self.stopped = threading.Event()
        self.stop_requested = threading.Event()     # the first SIGINT or SIGTERM sets it
        self.stop_mode: str | None = None

        if resume_dir is not None:
            self.dir = Path(resume_dir).expanduser().resolve()
            if not (self.dir / "run.db").is_file():
                raise SystemExit(f"No stored run database at {self.dir / 'run.db'}.")
            self.store = Store(self.dir / "run.db")
            self.run_id = str(self.store.read("/run/id") or self.dir.name)
            is_offline = bool(getattr(args, "offline", False))
            if is_offline:
                self._seed_accounts(True)
            prepare_resume(self.store, offline=is_offline)
        else:
            self.run_id = "r-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            self.dir = HOME / "runs" / self.run_id
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
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
            self._seed_accounts(args.offline or getattr(args, "dry_run", False) or getattr(args, "preview", False),
                                agent_provider=getattr(args, "provider", "agy"),
                                cascade=getattr(args, "cascade", None))
            if getattr(args, "dry_run", False) or getattr(args, "preview", False):
                from .store import Changes as _Changes
                cfg_changes = _Changes()
                if getattr(args, "dry_run", False):
                    # A dry run rehearses the whole pipeline and never lands anything.
                    # The simulator answers every model, the CI service is the local
                    # lane, and the merge stage reports what it would do.
                    cfg_changes.set("config", "dry_run", True).set("config", "ci_service", "local")
                if getattr(args, "preview", False):
                    cfg_changes.set("config", "preview", True)
                self.store.commit("config", cfg_changes, {})
            # A DAG can name its own repository with a "repo" key. Resolve that path
            # here so --base and --branch validate against the repo the run targets.
            flag_repo = Path(target) if args.repo else None
            if flag_repo is None and getattr(args, "dag", None):
                from . import dag_loader
                try:
                    named = str(dag_loader.load_document(
                        args.dag.expanduser().resolve()).get("repo") or "").strip()
                except Exception:               # a bad DAG still fails at plan.load
                    named = ""
                if named not in ("", "."):
                    flag_repo = Path(named).expanduser().resolve()
            if getattr(args, "base", None) or getattr(args, "branch", None):
                apply_branch_flags(self.store, flag_repo,
                                   getattr(args, "base", None), getattr(args, "branch", None),
                                   dry_run=bool(getattr(args, "dry_run", False)))
            if getattr(args, "cascade", None):
                is_offline = bool(args.offline or getattr(args, "dry_run", False) or getattr(args, "preview", False))
                install_cascade(self.store, getattr(args, "cascade"), offline=is_offline)

        completer = make_completer(
            self.store,
            offline=bool(getattr(args, "offline", False) or getattr(args, "dry_run", False)),
        )
        self.engine = Engine(self.store, completer)
        self.runtime = self._runtime()
        self.engine.runtime = self.runtime
        if resume_dir is not None:
            phase = self.store.read("/run/phase")
            if phase == "execute":
                self.runtime.start_execution()
            elif phase == "bootstrap" and self.store.read("/bootstrap/status") == "running":
                self.runtime.start_bootstrap()
        elif getattr(args, "dag", None):
            load_payload: dict = {
                "path": str(args.dag.expanduser().resolve()),
                "start": bool(getattr(args, "start", False)),
            }
            if (getattr(args, "dry_run", False) or getattr(args, "preview", False)) and not getattr(args, "repo", None):
                from . import worktrees as wt
                rehearsal_repo = self.dir / "preview-repo"
                wt.init_repo(rehearsal_repo)
                if wt.head(rehearsal_repo) is None:
                    (rehearsal_repo / "README.md").write_text("# Dry-run preview repository\n")
                    wt.commit_all(rehearsal_repo, "Initial preview commit")
                load_payload["target"] = str(rehearsal_repo)
            self.engine.submit({"id": "cli-dag", "type": "plan.load", "payload": load_payload})

        tailnet = None if args.no_tailscale else netinfo.detect_tailscale()
        self.auth = Auth()
        public_paths = frozenset()
        if getattr(args, "publish_preview", False):
            public_paths = frozenset({"/api/v1/preview", "/api/v1/health"})
        self.app = App(self.store, self.engine, self.auth,
                       extra_info=lambda: {"pid": os.getpid(), "run_dir": str(self.dir)},
                       link_bases=lambda: [{"label": l.label, "url": l.url} for l in self.links if l.verified],
                       fleet_reader=lambda: metrics.read_tracking(self.dir),
                       base_path=getattr(args, "base_path", "") or "",
                       public_paths=public_paths)
        self.server = Server(self.app, netinfo.hosts(tailnet), args.port).start()
        self.links = netinfo.links(self.server.port, self.run_id, tailnet)
        Control(self.links[0].url, self.auth.token, self.run_id, os.getpid()).save(self.dir / "control.json")
        self._publish_registry(args)

    def _publish_registry(self, args) -> None:
        """Record where this run's preview is, so a proxy can forward to it.

        The record carries no token: the proxy authenticates its own callers and
        needs only the address. A preview run publishes by default; any other run
        publishes only when --registry names a path.
        """
        path = getattr(args, "registry", None)
        if path is None:
            if not getattr(args, "preview", False):
                return
            # A test must never publish to the live record. A suite run under pytest or
            # unittest inherits no --registry, so without this guard it overwrote the
            # founder's dashboard record with a temp dag and a dead pid (2026-11-17, and
            # again the same day from a branch cut before the first fix). An explicit
            # --registry still publishes, so a real preview and its test can both work.
            if os.environ.get("PYTEST_CURRENT_TEST") or "unittest" in sys.modules:
                return
            path = HOME / "preview.json"
        path = Path(path).expanduser()
        dag = getattr(args, "dag", None)
        record = {
            "url": f"http://127.0.0.1:{self.server.port}{self.app.base_path}",
            "port": self.server.port,
            "base_path": self.app.base_path,
            "run_id": self.run_id,
            "pid": os.getpid(),
            "dag": str(dag.expanduser().resolve()) if dag else None,
            "preview": bool(getattr(args, "preview", False)),
            "published": sorted(self.app.public_paths),
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n")

    def _seed_accounts(self, offline: bool, agent_provider: str = "agy",
                       cascade: str | None = None) -> None:
        """Enable the provider that dispatches agents and every cascade provider.

        FOUNDER ORDER 2026-11-13: no Claude model in any subagent. The default
        is therefore agy, not claude. A Claude run needs the founder to name it.
        An explicit cascade enables every provider it names: the founder cascade
        runs on claude and agy, and a JSON cascade names its own providers.
        """
        from . import providers
        from .store import Changes
        named = cascade_providers(cascade)
        changes = Changes()
        for name in providers.catalog()["providers"]:
            account = providers.blank_account(name)
            account["enabled"] = name == "simulator" if offline else (
                name in (agent_provider, "simulator") or name in named)
            changes.set("accounts", name, account)
        self.store.commit("account.status", changes)

    def _runtime(self):
        from .supervisor import Supervisor
        # The supervisor reads the repository from state.run.target, which the user sets in
        # the stack step. Offline runs enable only the simulator account, so selection picks it.
        return Supervisor(self.store, RunHooks(self), self.dir, parent_pid=os.getpid(),
                          ci_service=getattr(self.args, "ci_service", "ci-conductor"))

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
        self.runtime.tracking.close()
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


def serve(args: argparse.Namespace, *, resume_dir: Path | None = None) -> int:
    run = Run(args, resume_dir=resume_dir)
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


def resume_command(args: argparse.Namespace) -> int:
    target_dir = getattr(args, "run_dir", None)
    if target_dir is None:
        runs = sorted((HOME / "runs").glob("r-*"), key=lambda p: p.stat().st_mtime)
        if not runs:
            if getattr(args, "if_exists", False):
                print("No stored run found; exiting cleanly.")
                return 0
            raise SystemExit("No run found. Start one with: just-build-it")
        target_dir = runs[-1]
    elif not (Path(target_dir).expanduser() / "run.db").is_file() and getattr(args, "if_exists", False):
        print(f"No stored run at {target_dir}; exiting cleanly.")
        return 0
    return serve(args, resume_dir=target_dir)


def run_client(run_dir: Path | None) -> Client:
    """A protocol client for a running dashboard, or a clear exit when there is none."""
    path = (run_dir or latest_run_dir()) / "control.json"
    try:
        return Client.from_control(Control.load(path))
    except (OSError, ValueError, KeyError):
        raise SystemExit(f"No running dashboard at {path.parent}. Start one with: just-build-it")


def _fail(code: str, message: str) -> int:
    print(json.dumps({"error": {"code": code, "message": message}}), file=sys.stderr)
    return 1


def _check_send(args: argparse.Namespace) -> None:
    """Refuse a bad `send` before any server contact. One target only."""
    from .tracking import ID_RE
    message_mode = bool(args.all or args.agent is not None or args.text is not None)
    if not message_mode:
        if args.type is None:
            raise ClientError(400, "payload_invalid", "Give a protocol type, or --agent/--all with --text")
        return
    if args.all and args.agent is not None:
        raise ClientError(400, "payload_invalid", "Choose only one of --agent and --all")
    if args.type is not None:
        raise ClientError(400, "payload_invalid", "Use no protocol type with --agent or --all")
    if not args.all and args.agent is None:
        raise ClientError(400, "payload_invalid", "--text needs --agent or --all")
    if not args.text or not args.text.strip():
        raise ClientError(400, "payload_invalid", "An agent message needs --text")
    if args.agent is not None and args.agent != "all" and not ID_RE.match(args.agent):
        raise ClientError(400, "payload_invalid", f"Bad agent id: {args.agent!r}")


def _fleet_offline(run_dir: Path | None) -> int:
    """Read the SQLite store directly. The store must exist; a read never creates it."""
    from .tracking import Tracking
    run_dir = run_dir or latest_run_dir()
    if not (run_dir / "fleet.db").is_file():
        raise SystemExit(f"No internal tracking store at {run_dir / 'fleet.db'}")
    tr = Tracking(run_dir / "fleet.db")
    try:
        print(json.dumps(tr.fleet(), indent=2))
    finally:
        tr.close()
    return 0


def client_command(args: argparse.Namespace) -> int:
    """`status`, `links`, `fleet` and `send`: one protocol call; machine-readable errors on stderr."""
    if args.command == "fleet" and args.offline:
        return _fleet_offline(getattr(args, "run_dir", None))
    if args.command == "send":
        try:
            _check_send(args)
        except ClientError as error:
            return _fail(error.code, error.message)
    client = run_client(getattr(args, "run_dir", None))
    try:
        if args.command == "fleet":
            print(json.dumps(client.fleet(), indent=2))
        elif args.command == "send" and (args.all or args.agent is not None):
            print(json.dumps(client.send("message.send", {"target": args.agent or "all", "text": args.text,
                                                        "all_sessions": bool(args.all)})))
        elif args.command == "status":
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
        return _fail(error.code, error.message)
    except OSError:
        return _fail("unreachable", "The dashboard does not answer. The run has stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command in ("status", "links", "send", "fleet"):
        return client_command(args)
    if args.command == "resume":
        return resume_command(args)
    if args.command == "budget":
        from . import budget
        return budget.main(args)
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
