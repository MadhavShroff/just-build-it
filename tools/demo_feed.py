#!/usr/bin/env python3
"""Serve the dashboard against a seeded demo run that keeps moving.

Development helper only. It starts `jbi.server` with a real `jbi.store.Store`, seeds
the store from a realistic fixture for one phase, prints one-time dashboard links,
and commits plausible events on a fixed beat so that the live board can be watched
and screenshotted.

    python3 tools/demo_feed.py --phase execute --port 8787 --keys 3

stdout carries only the links, one per line. Diagnostics go to stderr.

`--engine auto` uses `jbi.engine.Engine(store, completer=None, runtime=None)` when that
module imports, and otherwise installs `DemoEngine` as `jbi.engine` before the server
is imported. `--engine demo` forces the demo engine, which answers every command with
plausible events and needs no model and no runtime.
"""

from __future__ import annotations

import argparse
import random
import signal
import sys
import tempfile
import threading
import time
import traceback
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jbi import models, protocol, rigor, waves  # noqa: E402
from jbi.protocol import ProtocolError  # noqa: E402
from jbi.store import Changes, Store, now  # noqa: E402

PHASES = protocol.PHASES
RUN_ID = "r-20261104-1502"
RUN_NAME = "lumen"
TMUX = f"tmux -L jbi-{RUN_ID} attach"
PROVIDER = "claude"
AGENT_STAGES = ("running", "verifying")        # task statuses that hold an agent session
ACTIVE = ("running", "verifying", "queued", "merging")
CHECKED_ROLES = ("acceptance", "verify", "review", "security")
rng = random.Random(7)


# ---- small helpers -----------------------------------------------------------------------

def ago(minutes: float) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fake_sha() -> str:
    return f"{rng.getrandbits(28):07x}"


def model_for(role: str, profile: str = "balanced", attempts: int = 0) -> str | None:
    if role not in rigor.AGENT_ROLES and role != "plan":
        return None
    tier = rigor.tier_for(profile, role, attempts)
    return models.catalog()["providers"][PROVIDER]["models"][tier]


def status_for(role: str) -> str:
    return {"implement": "running", "merge": "merging"}.get(role, "verifying")


def blank_stages(level: str, profile: str, attempts: int = 0) -> dict:
    return {s.name: {"status": "pending", "detail": "", "model": model_for(s.role, profile, attempts)}
            for s in rigor.pipeline(level)}


STAGE_PASS = {"implement": "Receipt PASS", "acceptance": "Acceptance command exited 0",
              "verify": "Brief met, checked from outside", "review": "No blocking findings",
              "security": "No findings", "merge": "Squash-merged on a pinned base"}
STAGE_RUN = {"implement": "Writing code from the brief", "acceptance": "Running the acceptance command",
             "verify": "Checking the result against the brief", "review": "Reviewing the diff",
             "security": "Scanning the diff", "merge": "Lane is testing the combined tree"}
FINDINGS = ("an unchecked None return in the handler", "a missing index on books.isbn",
            "a test that depends on wall-clock time", "an unescaped title in the shelf view",
            "a session cookie without the Secure flag")


# ---- the fixture ---------------------------------------------------------------------------

TASKS: list[dict] = [
    {"id": "schema", "title": "Database schema and migrations",
     "goal": "Create the SQLite schema for users, books, shelves and reading sessions, "
             "with forward-only migrations.",
     "deps": [], "acceptance": "uv run pytest tests/test_schema.py",
     "owns": ["app/db/schema.sql", "app/db/migrate.py"], "components": ["db"]},
    {"id": "design-system", "title": "Design tokens and base components",
     "goal": "Add colour, type and spacing tokens and the Button, Input and Card components.",
     "deps": [], "acceptance": "pnpm -C web test -- tokens",
     "owns": ["web/src/lib/ui/"], "components": ["web"]},
    {"id": "auth", "title": "Magic-link sign-in",
     "goal": "Email a one-time sign-in link and exchange it for a session cookie.",
     "deps": ["schema"], "acceptance": "uv run pytest tests/test_auth.py",
     "owns": ["app/auth.py", "app/mail.py"], "components": ["api", "mailer"]},
    {"id": "api-books", "title": "Books and shelves API",
     "goal": "Create, read, update and delete books and shelves, with ISBN lookup.",
     "deps": ["schema"], "acceptance": "uv run pytest tests/test_books.py",
     "owns": ["app/routes/books.py", "app/routes/shelves.py"], "components": ["api", "db"]},
    {"id": "ui-shell", "title": "App shell and routing",
     "goal": "Build the layout, navigation and route guards for signed-in pages.",
     "deps": ["design-system"], "acceptance": "pnpm -C web test -- shell",
     "owns": ["web/src/routes/+layout.svelte", "web/src/lib/nav/"], "components": ["web"]},
    {"id": "reading-log", "title": "Reading sessions and streaks",
     "goal": "Record reading sessions per book and compute daily streaks.",
     "deps": ["api-books", "auth"], "acceptance": "uv run pytest tests/test_sessions.py",
     "owns": ["app/routes/sessions.py", "app/streaks.py"], "components": ["api", "db"]},
    {"id": "search", "title": "Full-text search",
     "goal": "Search titles, authors and notes with SQLite FTS5 and a small result cache.",
     "deps": ["api-books"], "acceptance": "uv run pytest tests/test_search.py",
     "owns": ["app/search.py"], "components": ["api", "cache"]},
    {"id": "covers", "title": "Cover image fetcher",
     "goal": "Fetch covers from Open Library in a background worker and store them on the volume.",
     "deps": ["api-books"], "acceptance": "uv run pytest tests/test_covers.py",
     "owns": ["app/workers/covers.py"], "components": ["worker", "openlibrary", "blob"]},
    {"id": "csv-import", "title": "Goodreads CSV import",
     "goal": "Import a Goodreads CSV export as books, shelves and finished dates.",
     "deps": ["api-books"], "acceptance": "uv run pytest tests/test_import.py",
     "owns": ["app/workers/importer.py"], "components": ["worker", "queue"]},
    {"id": "library-view", "title": "Library and shelf views",
     "goal": "Show the library grid, shelf pages and the book detail sheet.",
     "deps": ["ui-shell", "api-books"], "acceptance": "pnpm -C web test -- library",
     "owns": ["web/src/routes/library/"], "components": ["web"]},
    {"id": "stats", "title": "Yearly reading stats",
     "goal": "Show books per month, pages per day and the longest streak for each year.",
     "deps": ["reading-log"], "acceptance": "uv run pytest tests/test_stats.py",
     "owns": ["app/routes/stats.py", "web/src/routes/stats/"], "components": ["api", "web"]},
    {"id": "deploy", "title": "Fly.io deploy and backups",
     "goal": "Deploy one machine with a volume, and back up SQLite continuously with Litestream.",
     "deps": ["library-view", "reading-log", "search"], "acceptance": "./scripts/smoke.sh",
     "owns": ["fly.toml", "litestream.yml", ".github/workflows/deploy.yml"],
     "components": ["api", "db", "blob"]},
]

MESSAGES = [
    ("user", "I want a calm little web app to track the books I read: shelves, reading streaks "
             "and a summary of each year."),
    ("assistant", "Nice. Is it only for you, or can friends sign in too?"),
    ("user", "Me and a few friends. Email sign-in, no passwords."),
    ("assistant", "Then I plan magic-link sign-in. Do you want to bring in your Goodreads history?"),
    ("user", "Yes, from the Goodreads CSV export. Covers would be nice as well."),
    ("assistant", "I will fetch covers from Open Library in the background and keep a local copy. "
                  "Any limit on hosting or budget?"),
    ("user", "The cheapest thing that works. Fly.io is fine."),
]
CHAT_REPLIES = (
    "Noted. I added that to the rough PRD. Anything else, or shall I plan?",
    "Good point. I will keep that as an assumption that you can change later.",
    "That is enough to plan. Select Stop questions and plan when you are ready.",
)

ROUGH_PRD = (
    "Lumen is a small, calm reading tracker for one person and a few friends.\n\n"
    "- Sign in with an emailed magic link. No passwords.\n"
    "- Add books by ISBN or by hand. Group them on shelves.\n"
    "- Log reading sessions. Show daily streaks.\n"
    "- Import a Goodreads CSV export.\n"
    "- Fetch covers from Open Library and keep a local copy.\n"
    "- Show a yearly summary: books per month, pages per day, longest streak.\n"
    "- Host as cheaply as possible on Fly.io."
)
PRD = {
    "name": RUN_NAME,
    "problem": "Reading apps are noisy and social-first. The user wants a quiet private tracker.",
    "users": ["The owner", "Up to ten invited friends"],
    "features": ["Magic-link sign-in", "Books and shelves", "Reading sessions and streaks",
                 "Goodreads CSV import", "Cover images", "Full-text search", "Yearly stats"],
    "constraints": ["Cheapest workable hosting", "No passwords", "Works on a phone"],
    "assumptions": ["Open Library is the cover source", "One Fly.io region is enough",
                    "SQLite handles ten users"],
    "components": ["web", "api", "worker", "db"],
}
STACK = {
    "summary": "SvelteKit · FastAPI · SQLite · Fly.io",
    "layers": [
        {"layer": "frontend", "choice": "SvelteKit, static adapter",
         "reason": "Ships plain files, so there is no server-rendering cost.", "cost": "free"},
        {"layer": "backend", "choice": "Python 3.12 · FastAPI",
         "reason": "Small and typed; pytest covers it quickly.", "cost": "free"},
        {"layer": "database", "choice": "SQLite + Litestream",
         "reason": "One file, with continuous backup to object storage.", "cost": "free"},
        {"layer": "email", "choice": "Resend free tier",
         "reason": "3,000 magic-link emails a month at no cost.", "cost": "free"},
        {"layer": "hosting", "choice": "Fly.io shared-cpu-1x",
         "reason": "You named Fly.io. One small machine and a 1 GB volume.", "cost": "low"},
    ],
}
ACCOUNTS = {
    "claude": {"enabled": True, "status": "ok", "detail": "Signed in. 3 models answer the probe.",
               "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]},
    "codex": {"enabled": True, "status": "failed",
              "detail": "Weekly usage limit reached. It resets on 2026-11-11.", "models": []},
    "agy": {"enabled": False, "status": "missing", "detail": "The agy CLI is not on PATH.", "models": []},
    "openai-compatible": {"enabled": False, "status": "unknown", "detail": "Not probed yet.",
                          "models": [], "key": "Stored in the keychain"},
    "simulator": {"enabled": True, "status": "ok", "detail": "Always available. Used for demos and tests.",
                  "models": ["sim-xl", "sim-l", "sim-s", "sim-xs"]},
}
NODES = [
    ("web", "Web app", "client", 0, "SvelteKit", "Static files served by the API machine."),
    ("api", "API", "service", 1, "FastAPI", "Owns every write. Cookie sessions."),
    ("worker", "Job worker", "worker", 2, "Python", "Runs cover fetches and CSV imports."),
    ("queue", "Job queue", "queue", 2, "SQLite jobs table", "At-least-once, polled every 2 s."),
    ("cache", "Result cache", "cache", 2, "In-process LRU", "Search results for 60 s."),
    ("db", "Database", "database", 3, "SQLite + FTS5", "Backed up by Litestream."),
    ("blob", "Cover store", "storage", 3, "Fly volume", "1 GB, covers as WebP."),
    ("openlibrary", "Open Library", "external", 3, "Covers API", "Rate-limited; cache every hit."),
    ("mailer", "Resend", "external", 3, "Email API", "Magic-link mail only."),
]
EDGES = [
    ("e-web-api", "web", "api", "REST /api/*", "HTTPS"),
    ("e-api-db", "api", "db", "reads and writes", "SQL"),
    ("e-api-cache", "api", "cache", "search lookups", "in-process"),
    ("e-api-queue", "api", "queue", "enqueue jobs", "SQL"),
    ("e-worker-queue", "worker", "queue", "claim jobs", "SQL"),
    ("e-worker-ol", "worker", "openlibrary", "fetch covers", "HTTPS"),
    ("e-worker-blob", "worker", "blob", "write images", "filesystem"),
    ("e-worker-db", "worker", "db", "update books", "SQL"),
    ("e-api-mailer", "api", "mailer", "send magic links", "HTTPS"),
    ("e-web-blob", "web", "blob", "cover images", "HTTPS"),
]

# Task progress in the execute phase: status, the current stage name, attempts.
EXECUTE = {
    "schema": ("merged", None, 0), "design-system": ("merged", None, 0),
    "auth": ("merged", None, 1), "api-books": ("merged", None, 0),
    "ui-shell": ("merging", "merge", 0), "reading-log": ("running", "implement", 0),
    "search": ("verifying", "verify", 0), "covers": ("failed", "acceptance", 2),
    "csv-import": ("ready", None, 0),
}

# Timeline seed: (minutes ago, first phase that shows it, kind, text, event type, meta).
TIMELINE: list[tuple] = [
    (52, "intake", "run", "Run created", "log.line", {}),
    (51, "intake", "account", "Claude: ok", "account.status", {"provider": "claude"}),
    (51, "intake", "account", "Codex (OpenAI): failed", "account.status", {"provider": "codex"}),
    (50, "intake", "intake", "Detail level set to guided", "run.config", {}),
    (48, "stack", "plan", "Plan ready: 12 tasks in 4 waves", "plan.updated", {"revision": 1}),
    (47, "stack", "stack", f"Proposed tech stack: {STACK['summary']}", "stack.proposed", {}),
    (45, "bootstrap", "stack", "Stack accepted. Bootstrapping ~/lumen", "bootstrap.status", {}),
    (45, "bootstrap", "phase", "Bootstrap started", "run.phase", {"phase": "bootstrap"}),
    (44, "bootstrap", "bootstrap", "git init -b main; wrote .gitignore and README.md", "bootstrap.status", {}),
    (43, "bootstrap", "bootstrap", "uv init: Python 3.12.7, pyproject.toml written", "bootstrap.status", {}),
    (42, "bootstrap", "bootstrap", "Installed fastapi, uvicorn, pytest and ruff", "bootstrap.status", {}),
    (39, "configure", "bootstrap", "Bootstrap passed", "bootstrap.status", {}),
    (39, "configure", "phase", "Choose rigor and models", "run.phase", {"phase": "configure"}),
    (37, "execute", "config", "Rigor high, models balanced", "run.config", {}),
    (36, "execute", "phase", "Build started", "run.phase", {"phase": "execute"}),
    (36, "execute", "session", "schema: implement started on claude-sonnet-5-5", "session.status",
     {"task": "schema"}),
    (35, "execute", "session", "design-system: implement started on claude-sonnet-5-5", "session.status",
     {"task": "design-system"}),
    (30, "execute", "stage", "schema passed acceptance", "task.stage", {"task": "schema"}),
    (27, "execute", "stage", "design-system passed review", "task.stage", {"task": "design-system"}),
    (25, "execute", "task", "schema merged at 4b1d2e0", "task.status", {"task": "schema"}),
    (23, "execute", "task", "design-system merged at 81ac3f9", "task.status", {"task": "design-system"}),
    (22, "execute", "session", "auth: implement started on claude-sonnet-5-5", "session.status",
     {"task": "auth"}),
    (21, "execute", "session", "api-books: implement started on claude-sonnet-5-5", "session.status",
     {"task": "api-books"}),
    (18, "execute", "stage", "auth failed review: the session cookie lacks the Secure flag",
     "task.stage", {"task": "auth"}),
    (16, "execute", "stage", "auth passed review on attempt 2", "task.stage", {"task": "auth"}),
    (14, "execute", "task", "api-books merged at c03e771", "task.status", {"task": "api-books"}),
    (13, "execute", "task", "auth merged at 5fd09b2", "task.status", {"task": "auth"}),
    (12, "execute", "wave", "Wave 1 complete", "log.line", {"wave": 1}),
    (11, "execute", "session", "reading-log: implement started on claude-sonnet-5-5", "session.status",
     {"task": "reading-log"}),
    (10, "execute", "session", "covers: implement started on claude-sonnet-5-5", "session.status",
     {"task": "covers"}),
    (8, "execute", "stage", "covers failed acceptance: HTTP 429 from the covers API", "task.stage",
     {"task": "covers"}),
    (7, "execute", "task", "covers failed after 2 attempts", "task.status", {"task": "covers"}),
    (6, "execute", "decision", "Decision needed: Choose the primary Fly.io region", "decision.opened",
     {"decision": "d-region"}),
    (5, "execute", "stage", "ui-shell passed security scan", "task.stage", {"task": "ui-shell"}),
    (4, "execute", "lane", "lane-1 is testing ui-shell on 9c1e2f4", "lane.status", {"task": "ui-shell"}),
    (3, "execute", "session", "search: verify started on claude-opus-5-5", "session.status",
     {"task": "search"}),
    (1.5, "execute", "architecture", "Architecture: API building, cover worker failed",
     "architecture.updated", {}),
    (0.5, "complete", "phase", "Every task is merged", "run.phase", {"phase": "complete"}),
]


# ---- architecture status -------------------------------------------------------------------

def node_status(tasks: dict, ids: list[str]) -> str:
    statuses = [tasks[t]["status"] for t in ids if t in tasks]
    if not statuses:
        return "planned"
    if any(s in ACTIVE for s in statuses):
        return "building"
    if "failed" in statuses:
        return "failed"
    if all(s == "merged" for s in statuses):
        return "built"
    return "building" if "merged" in statuses else "planned"


def edge_status(a: str, b: str) -> str:
    if "failed" in (a, b):
        return "failed"
    if a == b == "built":
        return "built"
    return "building" if {"building", "built"} & {a, b} else "planned"


def build_architecture(tasks: dict, revision: int = 1) -> dict:
    nodes = {nid: {"id": nid, "name": name, "kind": kind, "layer": layer, "tech": tech, "status": "planned",
                   "tasks": [t for t in tasks if nid in tasks[t]["components"]], "notes": notes}
             for nid, name, kind, layer, tech, notes in NODES}
    edges = {eid: {"id": eid, "from": a, "to": b, "label": label, "protocol": proto, "status": "planned"}
             for eid, a, b, label, proto in EDGES}
    graph = {"revision": revision, "nodes": nodes, "edges": edges}
    for node in nodes.values():
        node["status"] = node_status(tasks, node["tasks"])
    for edge in edges.values():
        edge["status"] = edge_status(nodes[edge["from"]]["status"], nodes[edge["to"]]["status"])
    return graph


def architecture_changes(state: dict, changes: Changes | None = None) -> Changes:
    """Add status changes for every node and edge whose linked tasks moved."""
    changes = changes or Changes()
    tasks, graph = state["plan"]["tasks"], state["architecture"]
    fresh = {nid: node_status(tasks, n.get("tasks", [])) for nid, n in graph["nodes"].items()}
    for nid, status in fresh.items():
        if graph["nodes"][nid]["status"] != status:
            changes.replace("architecture", "nodes", nid, "status", status)
    for eid, edge in graph["edges"].items():
        status = edge_status(fresh.get(edge["from"], "planned"), fresh.get(edge["to"], "planned"))
        if edge["status"] != status:
            changes.replace("architecture", "edges", eid, "status", status)
    return changes


# ---- state per phase -----------------------------------------------------------------------

def build_plan(config: dict) -> dict:
    plan = waves.compile_plan(TASKS, 1)
    plan.update(rough_prd=ROUGH_PRD, prd=PRD)
    for task in plan["tasks"].values():
        task["stages"] = blank_stages(config["rigor"], config["profile"])
        task["model"] = model_for("implement", config["profile"])
    return plan


def apply_execute_progress(plan: dict, config: dict) -> None:
    level, profile = config["rigor"], config["profile"]
    names = [s.name for s in rigor.pipeline(level)]
    for tid, (status, current, attempts) in EXECUTE.items():
        task = plan["tasks"][tid]
        task.update(status=status, attempts=attempts, stage=current)
        stages = blank_stages(level, profile, attempts)
        cut = len(names) if status == "merged" else (names.index(current) if current else 0)
        for name in names[:cut]:
            stages[name].update(status="passed", detail=STAGE_PASS[rigor.stage(level, name).role])
        if current:
            role = rigor.stage(level, current).role
            stages[current]["status"] = "failed" if status == "failed" else "running"
            stages[current]["detail"] = ("tests/test_covers.py::test_fetch_fallback: HTTP 429 from the covers API"
                                         if status == "failed" else STAGE_RUN[role])
            task["model"] = stages[current]["model"] or task["model"]
        if status == "merged":
            task["commit"] = fake_sha()
            task["receipt"] = f"BRIEF {tid} | {task['commit']} | PASS"
            task["stage"] = "merge"
        task["stages"] = stages


def complete_progress(plan: dict, config: dict) -> None:
    for tid, task in plan["tasks"].items():
        task.update(status="merged", stage="merge", commit=fake_sha())
        task["receipt"] = f"BRIEF {tid} | {task['commit']} | PASS"
        for stage in rigor.pipeline(config["rigor"]):
            task["stages"][stage.name].update(status="passed", detail=STAGE_PASS[stage.role])


def idle_lane(base: str) -> dict:
    return {"status": "idle", "task": None, "base": base, "head": None, "checks": [],
            "detail": "Waiting for a finished task"}


def session(task: str, role: str, model: str | None, started: str, status: str = "running") -> dict:
    return {"task": task, "role": role, "model": model, "provider": PROVIDER, "window": f"{task}:{role}",
            "status": status, "started": started, "ended": None if status == "running" else now()}


def build_state(phase: str) -> tuple[dict, list[tuple]]:
    """The seeded state document for `phase`, and the timeline lines to commit after it."""
    rank = PHASES.index(phase)
    target = str(Path.home() / RUN_NAME) if rank >= PHASES.index("bootstrap") else ""
    state = protocol.initial_state(RUN_ID, ago(52), name=RUN_NAME, mode="new", target=target,
                                   max_sessions=3, lanes=2, tmux=TMUX)
    state["run"]["phase"] = phase
    catalog = models.catalog()["providers"]
    state["accounts"] = {name: {"label": catalog[name]["label"], "checked": ago(51), **account}
                         for name, account in ACCOUNTS.items()}
    messages = [{"role": role, "text": text, "ts": ago(50 - 1.2 * i)} for i, (role, text) in enumerate(MESSAGES)]
    if phase == "intake":
        messages = [{**m, "ts": ago(len(MESSAGES) - i)} for i, m in enumerate(messages)]
    state["intake"]["messages"] = messages
    state["plan"]["rough_prd"] = ROUGH_PRD
    config = state["config"]
    if rank >= PHASES.index("stack"):
        state["plan"] = build_plan(config)
        state["stack"] = {**STACK, "accepted": rank >= PHASES.index("bootstrap")}
    if phase == "bootstrap":
        state["bootstrap"] = {"status": "running", "detail": "Installing backend packages", "commit": None}
        state["sessions"] = {"s-bootstrap-1": session("bootstrap", "bootstrap", model_for("plan"), ago(45))}
    if rank >= PHASES.index("configure"):
        base = fake_sha()
        state["bootstrap"] = {"status": "passed", "detail": "build and lint exit 0 in a fresh shell",
                              "commit": base}
        state["lanes"] = {"lane-1": idle_lane(base), "lane-2": idle_lane(base)}
    if phase == "execute":
        apply_execute_progress(state["plan"], config)
        state["sessions"] = {
            "s-reading-log-1": session("reading-log", "implement", model_for("implement"), ago(11)),
            "s-search-2": session("search", "verify", model_for("verify"), ago(3)),
        }
        state["lanes"]["lane-1"] = {
            "status": "testing", "task": "ui-shell", "base": "9c1e2f4", "head": fake_sha(),
            "checks": [{"name": "build", "status": "passed"}, {"name": "lint", "status": "passed"},
                       {"name": "test", "status": "running"}],
            "detail": "Squash-merged jbi/ui-shell onto 9c1e2f4; running test"}
        state["decisions"] = {"d-region": {
            "title": "Choose the primary Fly.io region",
            "body": "deploy needs one primary region for the machine and its volume. "
                    "Pick the region closest to most readers.",
            "options": ["bom (Mumbai)", "sin (Singapore)", "fra (Frankfurt)"],
            "status": "open", "answer": None, "owner": "demo", "task": "deploy"}}
        state["telemetry"] = {"tokens_in": 1_284_310, "tokens_out": 211_904, "cost_usd": None, "samples": 148}
    if phase == "complete":
        complete_progress(state["plan"], config)
        state["sessions"] = {
            "s-deploy-1": session("deploy", "implement", model_for("implement"), ago(9), "ended"),
            "s-stats-1": session("stats", "review", model_for("review"), ago(6), "ended"),
        }
        state["decisions"] = {"d-region": {
            "title": "Choose the primary Fly.io region", "body": "deploy needs one primary region.",
            "options": ["bom (Mumbai)", "sin (Singapore)", "fra (Frankfurt)"],
            "status": "answered", "answer": "bom (Mumbai)", "owner": "demo", "task": "deploy"}}
        state["telemetry"] = {"tokens_in": 3_902_118, "tokens_out": 604_377, "cost_usd": None, "samples": 412}
    if state["plan"]["tasks"]:
        state["architecture"] = build_architecture(state["plan"]["tasks"])
    return state, [line for line in TIMELINE if PHASES.index(line[1]) <= rank]


def seed_timeline(store: Store, lines: list[tuple]) -> None:
    """Commit the timeline seed with back-dated times, the way Store.log would."""
    shift = min(line[0] for line in lines) - 0.4 if lines else 0
    for minutes, _phase, kind, text, etype, meta in lines:
        entry = {"seq": store.seq + 1, "ts": ago(minutes - shift), "kind": kind, "text": text}
        entry.update({k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))})
        store.commit(etype, Changes().append("timeline", entry), {"kind": kind, **meta})


# ---- the demo engine -----------------------------------------------------------------------

class DemoEngine:
    """A stand-in for jbi.engine.Engine: same write path, canned results, no model."""

    def __init__(self, store: Store, completer: Any = None, runtime: Any = None):
        self.store = store
        self._busy = threading.Lock()
        self._handlers: dict[str, Callable[[dict], None]] = {
            name: getattr(self, "_" + name.replace(".", "_")) for name in protocol.COMMANDS}

    # the write path, identical in shape to Engine.submit
    def submit(self, body: Any) -> dict:
        cid, ctype, payload = protocol.validate_command(body)
        previous = self.store.command_result(cid)
        if previous is not None:
            return previous
        phases = protocol.COMMANDS[ctype].phases
        phase = self.store.read("/run/phase")
        if phase not in phases:
            raise ProtocolError("phase_invalid", f"{ctype} needs phase {' or '.join(phases)}; "
                                                 f"the run is in phase '{phase}'.",
                                expected=list(phases), actual=phase)
        self._current = ctype
        self._handlers[ctype](payload)
        result = {"protocol": protocol.PROTOCOL_VERSION, "accepted": True, "id": cid, "seq": self.store.seq}
        self.store.record_command(cid, ctype, protocol.redact(ctype, payload), result)
        return result

    # helpers
    def _phase(self, phase: str, text: str) -> None:
        self.store.log("phase", text, extra=Changes().replace("run", "phase", phase), etype="run.phase",
                       phase=phase)

    def _say(self, role: str, text: str, **extra: Any) -> None:
        message = {"role": role, "text": text, "ts": now(), **extra}
        self.store.commit("chat.message", Changes().append("intake", "messages", message), {"role": role})

    def _later(self, label: str, delay: float, work: Callable[[], None]) -> None:
        if not self._busy.acquire(blocking=False):
            raise ProtocolError("conflict", "The planner is busy. Wait for the current step to finish.",
                                busy=self.store.read("/run/busy"))
        self.store.commit("run.config", Changes().replace("run", "busy", {"label": label, "since": now()}))

        def body() -> None:
            try:
                time.sleep(delay)
                work()
            except Exception as error:
                traceback.print_exc()
                self.store.log("error", f"{label} failed: {error}")
            finally:
                self.store.commit("run.config", Changes().replace("run", "busy", None))
                self._busy.release()
        threading.Thread(target=body, name=f"demo-{label}", daemon=True).start()

    def _task(self, tid: str) -> dict:
        task = self.store.read(f"/plan/tasks/{tid}")
        if task is None:
            raise ProtocolError("not_found", f"No task {tid!r} in the plan.", task=tid)
        return task

    # intake
    def _intake_say(self, payload: dict) -> None:
        self._say("user", payload["text"])
        reply = CHAT_REPLIES[len(self.store.read("/intake/messages")) % len(CHAT_REPLIES)]
        self._later("Thinking", 1.2, lambda: self._say("assistant", reply, ready=True))

    def _intake_detail(self, payload: dict) -> None:
        self.store.log("intake", f"Detail level set to {payload['level']}",
                       extra=Changes().replace("intake", "detail", payload["level"]), etype="run.config")

    def _intake_import(self, payload: dict) -> None:
        self._say("user", payload["text"], imported=True)
        lines = [line.strip(" -*\t") for line in payload["text"].splitlines() if line.strip()]

        def convert() -> None:
            draft = "Imported list:\n\n" + "\n".join(f"- {line}" for line in lines)
            self.store.log("plan", "Imported list converted to a rough PRD",
                           extra=Changes().replace("plan", "rough_prd", draft), etype="plan.updated")
            self._say("assistant", f"I read {len(lines)} items and wrote a rough PRD. "
                                   "Stop questions when it looks right.", ready=True)
        self._later("Reading your list", 1.5, convert)

    def _intake_finish(self, payload: dict) -> None:
        if not self.store.read("/intake/messages"):
            raise ProtocolError("conflict", "Describe the project or import a list before you stop questions.")

        def finish() -> None:
            plan = build_plan(self.store.read("/config"))
            plan["revision"] = self.store.read("/plan/revision", 0) + 1
            changes = (Changes().replace("plan", plan).replace("architecture", build_architecture(plan["tasks"]))
                       .replace("run", "name", RUN_NAME))
            self.store.log("plan", f"Plan ready: {len(plan['order'])} tasks in {len(plan['waves'])} waves",
                           extra=changes, etype="plan.updated", revision=plan["revision"])
            self.store.log("stack", f"Proposed tech stack: {STACK['summary']}",
                           extra=Changes().replace("stack", {**STACK, "accepted": False}), etype="stack.proposed")
            self._phase("stack", "Stack proposal ready")
        self._later("Writing the plan", 2.0, finish)

    # stack
    def _stack_revise(self, payload: dict) -> None:
        def revise() -> None:
            layers = [dict(layer) for layer in STACK["layers"]]
            layers[0]["reason"] = f"Kept after your request: {payload['request']}"
            proposal = {**STACK, "layers": layers, "accepted": False}
            self.store.log("stack", f"Proposed tech stack: {STACK['summary']}",
                           extra=Changes().replace("stack", proposal), etype="stack.proposed")
        self._later("Revising the stack", 1.5, revise)

    def _stack_back(self, payload: dict) -> None:
        self._phase("intake", "Back to planning questions")

    def _stack_accept(self, payload: dict) -> None:
        target = Path(payload["target"]).expanduser()
        if not target.is_absolute():
            raise ProtocolError("payload_invalid", "The target must be an absolute path or start with ~.",
                                field="target")
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ProtocolError("conflict", f"{target} is not empty. Choose an empty or new folder.",
                                field="target")
        changes = (Changes().replace("stack", "accepted", True).replace("run", "target", str(target))
                   .replace("bootstrap", "status", "running").replace("bootstrap", "detail", "Starting")
                   .set("sessions", "s-bootstrap-1", session("bootstrap", "bootstrap", model_for("plan"), now())))
        if payload.get("name"):
            changes.replace("run", "name", payload["name"])
        self.store.log("stack", f"Stack accepted. Bootstrapping {target}", extra=changes, etype="bootstrap.status")
        self._phase("bootstrap", "Bootstrap started")

    # configure and execute
    def _run_configure(self, payload: dict) -> None:
        changes = (Changes().replace("config", "rigor", payload["rigor"])
                   .replace("config", "profile", payload["profile"]))
        self.store.log("config", f"Rigor {payload['rigor']}, models {payload['profile']}", extra=changes,
                       etype="run.config")

    def _run_start(self, payload: dict) -> None:
        self._phase("execute", "Build started")

    def _task_override(self, payload: dict) -> None:
        self._task(payload["task"])
        override = {k: payload[k] for k in ("rigor", "profile", "model") if payload.get(k)}
        text = ", ".join(f"{k} {v}" for k, v in override.items()) or "defaults"
        self.store.log("config", f"{payload['task']}: {text}",
                       extra=Changes().replace("plan", "tasks", payload["task"], "override", override),
                       etype="task.override", task=payload["task"])

    def _task_retry(self, payload: dict) -> None:
        task = self._task(payload["task"])
        if task["status"] not in ("failed", "paused"):
            raise ProtocolError("conflict", f"{task['id']} is {task['status']}; only a failed or paused task "
                                            "can be retried.", task=task["id"])
        with self.store.lock:
            start_task(self.store, task["id"], "Retry requested")

    def _task_pause(self, payload: dict) -> None:
        task = self._task(payload["task"])
        if task["status"] not in AGENT_STAGES:
            raise ProtocolError("conflict", f"{task['id']} is {task['status']}; only running work can pause.",
                                task=task["id"])
        changes = Changes().replace("plan", "tasks", task["id"], "status", "paused")
        end_sessions(self.store.snapshot()[1], task["id"], changes)
        self.store.log("task", f"{task['id']} paused after its current stage", extra=changes,
                       etype="task.status", task=task["id"])

    def _task_resume(self, payload: dict) -> None:
        task = self._task(payload["task"])
        if task["status"] != "paused":
            raise ProtocolError("conflict", f"{task['id']} is not paused.", task=task["id"])
        with self.store.lock:
            start_task(self.store, task["id"], "Resumed")

    def _decision_answer(self, payload: dict) -> None:
        decision = self.store.read(f"/decisions/{payload['decision']}")
        if decision is None or decision.get("status") != "open":
            raise ProtocolError("conflict", "That decision is not open.", decision=payload["decision"])
        changes = (Changes().replace("decisions", payload["decision"], "status", "answered")
                   .set("decisions", payload["decision"], "answer", payload["answer"]))
        self.store.log("decision", f"Answered: {decision['title']}", extra=changes, etype="decision.answered",
                       decision=payload["decision"])

    # accounts
    def _provider(self, name: str) -> dict:
        catalog = models.catalog()["providers"]
        if name not in catalog:
            raise ProtocolError("payload_invalid", f"Unknown provider {name!r}.", field="provider")
        return self.store.read(f"/accounts/{name}") or {
            "label": catalog[name]["label"], "enabled": False, "status": "unknown", "detail": "Not probed yet.",
            "checked": None, "models": []}

    def _account_set(self, payload: dict) -> None:
        account = self._provider(payload["provider"])
        account["enabled"] = payload["enabled"]
        if payload.get("api_key"):          # the demo keeps no key at all; it records only a note
            account["key"] = "Stored in memory for this run"
        self.store.log("account", f"{account['label']} {'enabled' if account['enabled'] else 'disabled'}",
                       extra=Changes().set("accounts", payload["provider"], account), etype="account.status",
                       provider=payload["provider"])

    def _account_probe(self, payload: dict) -> None:
        name = payload["provider"]
        self._provider(name)

        def probe() -> None:
            time.sleep(0.8)
            account = self._provider(name)
            ids = sorted(set(models.catalog()["providers"][name]["models"].values()))
            account.update(status="ok", detail=f"Probe passed. {len(ids)} models answer.", checked=now(),
                           models=ids)
            self.store.log("account", f"{account['label']}: ok", extra=Changes().set("accounts", name, account),
                           etype="account.status", provider=name)
        threading.Thread(target=probe, name=f"demo-probe-{name}", daemon=True).start()

    # architecture and stop
    def _architecture_refresh(self, payload: dict) -> None:
        with self.store.lock:
            state = self.store.snapshot()[1]
            changes = architecture_changes(state)
            text = "Architecture statuses refreshed" if changes else "Architecture checked: no change"
            self.store.log("architecture", text, extra=changes, etype="architecture.updated", trigger="refresh")

    def _run_stop(self, payload: dict) -> None:
        mode = payload["mode"]
        self.store.log("run", f"Stop requested: {mode}",
                       extra=Changes().replace("run", "stopping", {"mode": mode, "since": now()}),
                       etype="run.stopping", mode=mode)


# ---- shared progress helpers ---------------------------------------------------------------

def end_sessions(state: dict, tid: str, changes: Changes) -> None:
    for sid, item in state["sessions"].items():
        if item["task"] == tid and item["status"] == "running":
            changes.replace("sessions", sid, "status", "ended").set("sessions", sid, "ended", now())


def prune_sessions(state: dict, changes: Changes, keep: int = 3) -> None:
    ended = sorted((s for s in state["sessions"].items() if s[1]["status"] == "ended"),
                   key=lambda item: item[1].get("ended") or "")
    for sid, _ in ended[:-keep] if len(ended) > keep else []:
        changes.remove("sessions", sid)


_session_counter = iter(range(10, 10_000))


def open_session(state: dict, tid: str, role: str, model: str | None, changes: Changes) -> None:
    end_sessions(state, tid, changes)
    prune_sessions(state, changes)
    changes.set("sessions", f"s-{tid}-{next(_session_counter)}", session(tid, role, model, now()))


def start_task(store: Store, tid: str, why: str) -> None:
    """Move a task to a fresh implement stage. Call with the store lock held."""
    state = store.snapshot()[1]
    task = state["plan"]["tasks"][tid]
    level, profile = rigor.effective(task, state["config"])
    stages = blank_stages(level, profile, task["attempts"])
    first = rigor.pipeline(level)[0]
    stages[first.name].update(status="running", detail=STAGE_RUN[first.role])
    model = stages[first.name]["model"]
    changes = (Changes().replace("plan", "tasks", tid, "stages", stages)
               .replace("plan", "tasks", tid, "status", "running")
               .replace("plan", "tasks", tid, "stage", first.name)
               .set("plan", "tasks", tid, "model", model))
    open_session(state, tid, first.role, model, changes)
    state["plan"]["tasks"][tid]["status"] = "running"
    architecture_changes(state, changes)
    store.log("session", f"{tid}: {why}. Implement started on {model}",
              extra=changes, etype="session.status", task=tid)


# ---- the beat loop -------------------------------------------------------------------------

BOOTSTRAP_STEPS = [
    ("Creating the repository", "git init -b main; wrote .gitignore and README.md"),
    ("Setting up Python", "uv init: Python 3.12.7, pyproject.toml written"),
    ("Installing backend packages", "Installed fastapi, uvicorn, pytest and ruff"),
    ("Creating the web app", "pnpm create svelte@latest web --template skeleton"),
    ("Installing web tooling", "Installed eslint, prettier and vitest"),
    ("Writing the command file", "Wrote .jbi/commands.json: build, test, lint, format"),
    ("Running build in a fresh shell", "build: exit 0 in 6.1 s"),
    ("Running lint in a fresh shell", "lint: exit 0 in 2.4 s"),
]


class Beat:
    """One plausible change per tick, chosen from the current phase."""

    def __init__(self, store: Store, phase: str):
        self.store = store
        self.ticks = 0
        self.turn = 0
        self.phase = phase
        self.boot_step = 3 if phase == "bootstrap" else 0
        self.retried_failed = False

    def tick(self) -> None:
        self.ticks += 1
        with self.store.lock:
            state = self.store.snapshot()[1]
            phase = state["run"]["phase"]
            if phase != self.phase:
                self.phase, self.boot_step = phase, 0
            getattr(self, f"_{phase}")(state)

    # quiet phases
    def _quiet(self, state: dict) -> None:
        if self.ticks % 8 == 0:
            account = state["accounts"].get("claude")
            if account:
                self.store.log("account", "Claude: ok",
                               extra=Changes().replace("accounts", "claude", "checked", now()),
                               etype="account.status", provider="claude")

    _intake = _stack = _configure = _quiet

    def _complete(self, state: dict) -> None:
        return

    # bootstrap
    def _bootstrap(self, state: dict) -> None:
        if state["bootstrap"]["status"] != "running":
            return
        if self.boot_step < len(BOOTSTRAP_STEPS):
            detail, line = BOOTSTRAP_STEPS[self.boot_step]
            self.boot_step += 1
            self.store.log("bootstrap", line, extra=Changes().replace("bootstrap", "detail", detail),
                           etype="bootstrap.status")
            return
        base = fake_sha()
        changes = (Changes().replace("bootstrap", "status", "passed").replace("bootstrap", "commit", base)
                   .replace("bootstrap", "detail", "build and lint exit 0 in a fresh shell")
                   .replace("lanes", {"lane-1": idle_lane(base), "lane-2": idle_lane(base)}))
        end_sessions(state, "bootstrap", changes)
        self.store.log("bootstrap", "Bootstrap passed", extra=changes, etype="bootstrap.status")
        self.store.log("phase", "Choose rigor and models", extra=Changes().replace("run", "phase", "configure"),
                       etype="run.phase", phase="configure")

    # execute
    def _execute(self, state: dict) -> None:
        stopping = (state["run"].get("stopping") or {}).get("mode")
        if stopping in ("quit", "standdown", "kill"):
            return self._halt(state, stopping)
        tasks, order = state["plan"]["tasks"], state["plan"]["order"]
        if order and all(tasks[t]["status"] == "merged" for t in order):
            changes = Changes()
            for sid, item in state["sessions"].items():
                if item["status"] == "running":
                    changes.replace("sessions", sid, "status", "ended").set("sessions", sid, "ended", now())
            self.store.log("phase", "Every task is merged", extra=changes.replace("run", "phase", "complete"),
                           etype="run.phase", phase="complete")
            return
        active = [t for t in order if tasks[t]["status"] in ACTIVE]
        if active:
            tid = active[self.turn % len(active)]
            self.turn += 1
            self._advance(state, tid)
        state = self.store.snapshot()[1]
        if stopping != "finish":
            self._admit(state)
        self._telemetry(self.store.snapshot()[1], bool(active))

    def _halt(self, state: dict, mode: str) -> None:
        tasks = state["plan"]["tasks"]
        moving = [t for t in state["plan"]["order"] if tasks[t]["status"] in AGENT_STAGES]
        if not moving:
            return
        changes = Changes()
        for tid in moving:
            changes.replace("plan", "tasks", tid, "status", "paused")
            end_sessions(state, tid, changes)
        self.store.log("run", f"Stop mode {mode} complete", extra=changes, etype="run.stopping", mode=mode)

    def _admit(self, state: dict) -> None:
        tasks, config = state["plan"]["tasks"], state["config"]
        busy = sum(1 for t in tasks.values() if t["status"] in AGENT_STAGES)
        if busy >= config["max_sessions"]:
            return
        open_for = {d.get("task") for d in state["decisions"].values() if d.get("status") == "open"}
        if not self.retried_failed and self.ticks > 8:
            failed = [t for t in state["plan"]["order"] if tasks[t]["status"] == "failed"]
            if failed:
                self.retried_failed = True
                return start_task(self.store, failed[0], "Retry on a larger model")
        for tid in waves.ready_tasks(state["plan"]):
            if tid in open_for:
                if tasks[tid]["status"] != "decision":
                    self.store.log("task", f"{tid} waits for your decision",
                                   extra=Changes().replace("plan", "tasks", tid, "status", "decision"),
                                   etype="task.status", task=tid)
                continue
            running = [t for t, v in tasks.items() if v["status"] in ACTIVE]
            if waves.owns_conflict(state["plan"], tid, running):
                continue
            return start_task(self.store, tid, "Started")
        for tid, task in tasks.items():       # a decision that got its answer releases the task
            if task["status"] == "decision" and tid not in open_for:
                self.store.log("task", f"{tid} is ready", extra=Changes().replace("plan", "tasks", tid, "status",
                                                                                   "ready"),
                               etype="task.status", task=tid)

    def _advance(self, state: dict, tid: str) -> None:
        task = state["plan"]["tasks"][tid]
        level, profile = rigor.effective(task, state["config"])
        pipeline = rigor.pipeline(level)
        stages = task.get("stages") or {}
        current = next((s for s in pipeline if stages.get(s.name, {}).get("status") != "passed"), None)
        if current is None:
            return
        if current.role == "merge":
            return self._advance_merge(state, tid, current)
        stage = stages.get(current.name) or {"status": "pending", "detail": "", "model": None}
        path = ("plan", "tasks", tid, "stages", current.name)
        if stage["status"] == "pending":
            changes = (Changes().set(*path, {**stage, "status": "running", "detail": STAGE_RUN[current.role]})
                       .replace("plan", "tasks", tid, "stage", current.name)
                       .replace("plan", "tasks", tid, "status", status_for(current.role)))
            self._session_for(state, tid, current.role, stage.get("model"), changes)
            self.store.log("stage", f"{tid}: {current.label.lower()} started", extra=changes, etype="task.stage",
                           task=tid)
        elif stage["status"] == "failed":
            changes = Changes().replace("plan", "tasks", tid, "attempts", task["attempts"] + 1)
            self.store.commit("task.status", changes, {"task": tid})
            start_task(self.store, tid, f"Back to implement, attempt {task['attempts'] + 2}")
        elif current.role in CHECKED_ROLES and task["attempts"] < 1 and rng.random() < 0.15:
            finding = rng.choice(FINDINGS)
            self.store.log("stage", f"{tid} failed {current.label.lower()}: {finding}",
                           extra=Changes().set(*path, {**stage, "status": "failed", "detail": finding}),
                           etype="task.stage", task=tid)
        else:
            changes = Changes().set(*path, {**stage, "status": "passed", "detail": STAGE_PASS[current.role]})
            following = pipeline[pipeline.index(current) + 1]
            if following.role == "merge":
                changes.replace("plan", "tasks", tid, "status", "queued")
                end_sessions(state, tid, changes)
            else:
                nxt = stages.get(following.name) or {"status": "pending", "detail": "", "model": None}
                changes.set("plan", "tasks", tid, "stages", following.name,
                            {**nxt, "status": "running", "detail": STAGE_RUN[following.role]})
                changes.replace("plan", "tasks", tid, "stage", following.name)
                changes.replace("plan", "tasks", tid, "status", status_for(following.role))
                self._session_for(state, tid, following.role, nxt.get("model"), changes)
            self.store.log("stage", f"{tid} passed {current.label.lower()}", extra=changes, etype="task.stage",
                           task=tid)

    def _session_for(self, state: dict, tid: str, role: str, model: str | None, changes: Changes) -> None:
        if role in rigor.AGENT_ROLES:
            open_session(state, tid, role, model, changes)
            changes.set("plan", "tasks", tid, "model", model)
        else:
            end_sessions(state, tid, changes)

    def _advance_merge(self, state: dict, tid: str, stage: rigor.Stage) -> None:
        lanes = state["lanes"]
        lane_id = next((lid for lid, lane in lanes.items() if lane.get("task") == tid), None)
        path = ("plan", "tasks", tid, "stages", stage.name)
        if lane_id is None:
            free = next((lid for lid, lane in sorted(lanes.items()) if lane["status"] == "idle"), None)
            if free is None:
                return
            base = lanes[free]["base"] or fake_sha()
            level = rigor.effective(state["plan"]["tasks"][tid], state["config"])[0]
            checks = [{"name": c, "status": "pending"} for c in rigor.LEVELS[level]["lane_checks"]]
            lane = {"status": "testing", "task": tid, "base": base, "head": fake_sha(), "checks": checks,
                    "detail": f"Squash-merged jbi/{tid} onto {base}"}
            changes = (Changes().replace("lanes", free, lane)
                       .set(*path, {"status": "running", "detail": STAGE_RUN["merge"], "model": None})
                       .replace("plan", "tasks", tid, "stage", stage.name)
                       .replace("plan", "tasks", tid, "status", "merging"))
            self.store.log("lane", f"{free} is testing {tid} on {base}", extra=changes, etype="lane.status", task=tid)
            return
        lane = lanes[lane_id]
        pending = next((i for i, c in enumerate(lane["checks"]) if c["status"] != "passed"), None)
        if pending is not None:
            check = lane["checks"][pending]
            status = "running" if check["status"] == "pending" else "passed"
            changes = Changes().replace("lanes", lane_id, "checks", pending, "status", status)
            changes.replace("lanes", lane_id, "detail", f"{check['name']} {'running' if status == 'running' else 'passed'}")
            self.store.commit("lane.status", changes, {"task": tid, "lane": lane_id})
            return
        if lane["status"] == "testing":
            self.store.commit("lane.status", Changes().replace("lanes", lane_id, "status", "merging")
                              .replace("lanes", lane_id, "detail", "Base unchanged. Squash-merging the verified head"),
                              {"task": tid, "lane": lane_id})
            return
        sha = fake_sha()
        changes = (Changes().set(*path, {"status": "passed", "detail": f"Squash-merged at {sha}", "model": None})
                   .replace("plan", "tasks", tid, "status", "merged")
                   .replace("plan", "tasks", tid, "commit", sha)
                   .replace("plan", "tasks", tid, "receipt", f"BRIEF {tid} | {sha} | PASS")
                   .replace("lanes", lane_id, idle_lane(sha)))
        state["plan"]["tasks"][tid]["status"] = "merged"
        for other in waves.ready_tasks(state["plan"]):
            if state["plan"]["tasks"][other]["status"] == "blocked":
                changes.replace("plan", "tasks", other, "status", "ready")
        architecture_changes(state, changes)
        self.store.log("task", f"{tid} merged at {sha}", extra=changes, etype="task.status", task=tid)
        wave = state["plan"]["tasks"][tid]["wave"]
        members = state["plan"]["waves"][wave - 1] if 0 < wave <= len(state["plan"]["waves"]) else []
        if members and all(state["plan"]["tasks"][m]["status"] == "merged" for m in members):
            self.store.log("wave", f"Wave {wave} complete", wave=wave)
        if rng.random() < 0.5:
            self.store.log("architecture", f"Architecture: {tid} merged, statuses updated",
                           etype="architecture.updated", trigger="task.merged")

    def _telemetry(self, state: dict, working: bool) -> None:
        if not working:
            return
        tele = state["telemetry"]
        changes = (Changes().replace("telemetry", "tokens_in", tele["tokens_in"] + rng.randint(2_000, 18_000))
                   .replace("telemetry", "tokens_out", tele["tokens_out"] + rng.randint(300, 3_500))
                   .replace("telemetry", "samples", tele["samples"] + 1))
        self.store.commit("telemetry.sample", changes, {})


# ---- entry point ---------------------------------------------------------------------------

def load_engine(choice: str) -> tuple[type, str]:
    """Return the engine class and its kind. Installs DemoEngine as jbi.engine when needed."""
    if choice != "demo":
        try:
            from jbi.engine import Engine
            return Engine, "real"
        except Exception as error:      # other agents may still be writing engine dependencies
            if choice == "real":
                raise
            print(f"demo_feed: jbi.engine does not import ({error}); using DemoEngine", file=sys.stderr)
    import jbi
    module = types.ModuleType("jbi.engine")
    module.Engine = DemoEngine
    sys.modules["jbi.engine"] = module
    jbi.engine = module
    return DemoEngine, "demo"


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--phase", choices=PHASES, default="execute")
    parser.add_argument("--keys", type=int, default=1, help="number of one-time links to print")
    parser.add_argument("--interval", type=float, default=1.5, help="seconds between live events")
    parser.add_argument("--static", action="store_true", help="seed only; commit no live events")
    parser.add_argument("--db", type=Path, help="run database (default: a new temporary file)")
    parser.add_argument("--engine", choices=("auto", "real", "demo"), default="auto")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    engine_class, kind = load_engine(args.engine)
    from jbi.server import App, Auth, Server

    db = args.db or Path(tempfile.mkdtemp(prefix="jbi-demo-")) / "run.db"
    state, lines = build_state(args.phase)
    store = Store(db, initial=state)
    if store.seq == 1:
        seed_timeline(store, lines)
    engine = engine_class(store, completer=None, runtime=None)
    auth = Auth()
    server = Server(App(store, engine, auth), [args.host], args.port).start()
    for _ in range(max(1, args.keys)):
        print(f"http://{args.host}:{server.port}/#k={auth.new_key()}")
    sys.stdout.flush()
    print(f"demo_feed: engine={kind} phase={args.phase} db={db} live={not args.static}", file=sys.stderr)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    beat = Beat(store, store.read("/run/phase"))
    while not stop.wait(args.interval):
        if args.static:
            continue
        try:
            beat.tick()
        except Exception:     # a bad beat must not stop the demo server
            traceback.print_exc()
    server.stop()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
