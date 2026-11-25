"""Reclaim orphans left by a killed run (REQUIREMENT-resume section 2).

Orphans come in three shapes: tmux windows on the run's private tmux server
(`tmux -L jbi-<run-id>`), task worktrees under `run_dir / "worktrees" / <task>`,
and GATE jobs whose ids the run recorded in state. Resume finds each orphan by
run id and reclaims it. A live run is never reclaimed (2.2). A reclaim that
cannot happen is recorded and the resume continues (2.5).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import worktrees as wt


@dataclass
class OrphanReport:
    """What resume found and what it did about it."""
    tmux_windows_killed: list[str] = field(default_factory=list)
    tmux_windows_kept: list[dict] = field(default_factory=list)
    tmux_server_killed: bool = False
    worktrees_removed: list[str] = field(default_factory=list)
    worktrees_kept: list[dict] = field(default_factory=list)
    branches_removed: list[str] = field(default_factory=list)
    branches_kept: list[dict] = field(default_factory=list)
    gate_jobs_stopped: list[str] = field(default_factory=list)
    gate_jobs_kept: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tmux_windows_killed": list(self.tmux_windows_killed),
            "tmux_windows_kept": list(self.tmux_windows_kept),
            "tmux_server_killed": self.tmux_server_killed,
            "worktrees_removed": list(self.worktrees_removed),
            "worktrees_kept": list(self.worktrees_kept),
            "branches_removed": list(self.branches_removed),
            "branches_kept": list(self.branches_kept),
            "gate_jobs_stopped": list(self.gate_jobs_stopped),
            "gate_jobs_kept": list(self.gate_jobs_kept),
            "notes": list(self.notes),
        }


# ---- liveness (2.2) ----------------------------------------------------------------


def pid_alive(pid: int | None) -> bool:
    """Return True when `pid` names a live, non-zombie process.

    Read /proc/<pid>/stat directly so we never rely on a name pattern; a zombie
    answers kill(0) but runs nothing.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True    # owned by another user: err on the side of "live"
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    rest = text.rsplit(")", 1)
    if len(rest) < 2:
        return False
    state = rest[1].lstrip().split()[0] if rest[1].lstrip() else ""
    return state != "Z"


def control_pid(run_dir: Path) -> int | None:
    """Read the original run's pid from `control.json`. None when absent or stale."""
    path = run_dir / "control.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    pid = raw.get("pid")
    return int(pid) if isinstance(pid, int) else None


# ---- tmux (2.1, 2.2, 2.5) ----------------------------------------------------------


def tmux_socket(run_id: str) -> str:
    return f"jbi-{run_id}"


def list_tmux_windows(socket: str) -> list[str] | None:
    """Names of windows on the run's private tmux server, or None when no server.

    A non-zero exit that is NOT "no server" (e.g. a permission error) also
    returns None: we cannot list, so we cannot prove which windows to kill.
    """
    if shutil.which("tmux") is None:
        return None
    proc = subprocess.run(
        ["tmux", "-L", socket, "list-windows", "-a", "-F", "#{window_name}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").lower() + (proc.stdout or "").lower()
        if "no server" in err or "can't find session" in err or "no sessions" in err:
            return []
        return None
    return [name for name in proc.stdout.splitlines() if name.strip()]


def _kill_window(socket: str, name: str) -> bool:
    proc = subprocess.run(
        ["tmux", "-L", socket, "kill-window", "-t", f"jbi:{name}"],
        capture_output=True, text=True,
    )
    return proc.returncode == 0


def kill_tmux_server(socket: str) -> bool:
    if shutil.which("tmux") is None:
        return False
    proc = subprocess.run(["tmux", "-L", socket, "kill-server"],
                          capture_output=True, text=True)
    socket_path = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / socket
    try:
        socket_path.unlink(missing_ok=True)
    except OSError:
        pass
    return proc.returncode == 0


def reclaim_tmux(run_id: str, original_pid: int | None, report: OrphanReport) -> None:
    """Reclaim the run's private tmux server when the original pid is dead.

    Never fails: any window that cannot be reclaimed is recorded (2.5). The
    control window is auto-started by the supervisor with a `kill -0 <pid>` watch
    that ends the server when the parent dies, so a dead parent usually means
    the server is already gone.
    """
    socket = tmux_socket(run_id)
    if pid_alive(original_pid):
        report.tmux_windows_kept.append({"reason": "run pid still live", "pid": original_pid})
        return
    names = list_tmux_windows(socket)
    if names is None:
        report.tmux_windows_kept.append({"reason": "cannot list windows on the run's tmux server",
                                         "socket": socket})
        return
    if not names:
        report.tmux_server_killed = kill_tmux_server(socket)
        return
    failed: list[str] = []
    for name in names:
        if _kill_window(socket, name):
            report.tmux_windows_killed.append(name)
        else:
            failed.append(name)
    for name in failed:
        report.tmux_windows_kept.append({"window": name, "reason": "tmux kill-window failed"})
    if kill_tmux_server(socket):
        report.tmux_server_killed = True
    elif failed:
        report.notes.append(f"tmux server {socket} not killed; {len(failed)} window(s) resisted")


# ---- worktrees (2.3) --------------------------------------------------------------


def _unlink_repo_dep_symlinks(repo: Path, path: Path) -> None:
    """Remove `node_modules` and `dist` link-populated directories that point into `repo`."""
    repo_real = Path(repo).resolve()
    wt_real = Path(path).resolve()
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in list(dirs) + list(files):
            if name not in ("node_modules", "dist"):
                continue
            cand = Path(base) / name
            try:
                if cand.is_symlink():
                    real_t = Path(os.path.realpath(cand))
                    if real_t.is_relative_to(repo_real) or real_t.is_relative_to(wt_real):
                        cand.unlink()
                        if name in dirs:
                            dirs.remove(name)
                elif cand.is_dir():
                    entries = list(os.scandir(cand))
                    if entries and all(
                        (e.is_symlink() and (
                            Path(os.path.realpath(e.path)).is_relative_to(repo_real)
                            or Path(os.path.realpath(e.path)).is_relative_to(wt_real)
                        )) or (name == "node_modules" and e.name in (".vite", ".vite-temp", ".cache"))
                        for e in entries
                    ):
                        shutil.rmtree(cand)
                        if name in dirs:
                            dirs.remove(name)
            except (OSError, ValueError):
                pass


def _worktree_contents_committed(repo: Path, path: Path) -> tuple[bool, str]:
    """Prove every byte in the worktree is reachable in a commit.

    Step 1: `git status --ignored` must be empty. A dirty or ignored path means a
    half-written file we must not lose.

    Step 2: HEAD exists (the task's first commit landed), the worktree HEAD is
    reachable from the branch, and the working tree equals HEAD.

    Returns (ok, why).
    """
    try:
        _unlink_repo_dep_symlinks(repo, path)
        git_dir, common = wt.pin_git_metadata(path, repo)
        out = wt.trusted_git(git_dir, common, path, "status", "--ignored", "--porcelain", "-uall", result=True)
    except (OSError, wt.GitError) as error:
        return False, f"commit proof refused: {str(error)[:200]}"
    if out.returncode != 0:
        return False, f"git status failed: {(out.stderr or out.stdout).strip()[:200]}"
    if out.stdout.strip():
        return False, "uncommitted or ignored paths present"
    head = wt.head(path, "HEAD")
    if head is None:
        # An empty task worktree with no commit yet: nothing to lose beyond the
        # worktree plumbing. Refuse removal anyway; the original brief aborted
        # before the implementer committed, and someone may have placed an
        # untracked scratch file that `git status --ignored` would have shown.
        return False, "no HEAD commit in worktree"
    branch = wt.git(path, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    if not branch:
        return False, "detached HEAD"
    if wt.head(path, branch) != head:
        return False, "worktree HEAD is not the branch head"
    tree_head = wt.tree_of(path, head)
    tree_wt = wt.git(path, "write-tree")
    if tree_head != tree_wt:
        return False, "working tree differs from HEAD"
    return True, "clean and committed"


def _leased_worktrees(run_dir: Path, root: Path) -> dict[str, tuple[str, str]] | None:
    """Map real worktree path to (kind, task id) for every leased task.

    Return an empty map when no lease store exists. Return None when the store
    exists and cannot be read. A lease path counts only when it equals
    `run_dir/worktrees/<task>`. Any other path is not trusted.
    """
    store = Path(run_dir) / "fleet.db"
    if not store.exists():
        return {}
    from .tracking import Tracking  # local import: open the store only when it exists
    try:
        tracking = Tracking(store)
    except Exception:
        return None
    try:
        fleet = tracking.fleet()
    except Exception:
        return None
    finally:
        tracking.close()
    leased: dict[str, tuple[str, str]] = {}
    for lease in fleet.get("leases", []):
        task_id = str(lease.get("task_id") or "")
        if not task_id or "/" in task_id or task_id in (".", ".."):
            continue
        expected = os.path.realpath(root / task_id)
        if os.path.realpath(str(lease.get("worktree") or "")) != expected:
            continue
        kind = "live" if lease.get("released_at") is None else "parked"
        if kind == "parked" and leased.get(expected, ("", ""))[0] == "live":
            continue
        leased[expected] = (kind, task_id)
    return leased


def reclaim_worktrees(repo: Path | None, run_dir: Path, report: OrphanReport) -> None:
    """Remove task worktrees that are clean against their branch (2.3).

    Each orphan lives at `run_dir / "worktrees" / <task>`. Removal calls into
    `wt.remove_worktree`, which uses `git worktree remove` and never `rm -rf`.
    A tree with a live or released lease is never removed, clean or dirty.
    """
    if repo is None or not (Path(repo) / ".git").exists():
        report.notes.append("no repository to verify against; no worktree removed")
        return
    root = Path(run_dir) / "worktrees"
    if not root.is_dir():
        return
    leased = _leased_worktrees(Path(run_dir), root)
    if leased is None:
        report.notes.append("lease store unreadable; no worktree removed")
        for child in sorted(root.iterdir()):
            if child.is_dir():
                report.worktrees_kept.append({"path": str(child), "reason": "lease store unreadable"})
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        lease = leased.get(os.path.realpath(child))
        if lease is not None:
            kind, task_id = lease
            report.worktrees_kept.append({"path": str(child), "reason": f"{kind} lease for task {task_id}"})
            continue
        if not (child / ".git").exists():
            report.worktrees_kept.append({"path": str(child), "reason": "not a git worktree"})
            continue
        ok, why = _worktree_contents_committed(Path(repo), child)
        if not ok:
            report.worktrees_kept.append({"path": str(child), "reason": why})
            continue
        try:
            wt.remove_worktree(Path(repo), child)
            if child.exists():
                report.worktrees_kept.append({"path": str(child), "reason": "git worktree remove left files"})
            else:
                report.worktrees_removed.append(str(child))
        except Exception as error:
            report.worktrees_kept.append({"path": str(child), "reason": f"{type(error).__name__}: {error}"})


# ---- GATE jobs (2.4) -------------------------------------------------------------


def _gateids_from_state(record: dict | None) -> list[str]:
    if not isinstance(record, dict):
        return []
    raw = record.get("gate_jobs") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, (str, int))]


def reclaim_gate_jobs(gate_ids: list[str], report: OrphanReport, *, gatectl_path: str | None = None) -> None:
    """Stop each recorded GATE job through `gatectl stop`, never directly (2.4)."""
    if not gate_ids:
        return
    binary = gatectl_path or shutil.which("gatectl")
    if binary is None:
        for gid in gate_ids:
            report.gate_jobs_kept.append({"id": gid, "reason": "gatectl not on PATH"})
        return
    for gid in gate_ids:
        proc = subprocess.run([binary, "stop", str(gid)], capture_output=True, text=True)
        if proc.returncode == 0:
            report.gate_jobs_stopped.append(gid)
        else:
            report.gate_jobs_kept.append({"id": gid,
                                          "reason": f"gatectl stop rc={proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}"})


# ---- the orchestrator (1.3, 2.1, 2.5) ---------------------------------------------


def _state_read(run_dir: Path) -> dict:
    """Read lightweight fields I need from the run's store, without opening it.

    The store is a SQLite file opened by the runtime; this pass needs only the
    run id (already known), the target repo, and any recorded GATE job ids. Use
    the saved initial-state JSON if present, else walk `control.json` only.
    """
    state_path = Path(run_dir) / "initial-state.json"
    if state_path.is_file():
        try:
            return json.loads(state_path.read_text())
        except (OSError, ValueError):
            pass
    return {}


def write_resume_record(run_dir: Path, report: OrphanReport) -> Path:
    path = Path(run_dir) / "resume-record.json"
    path.write_text(json.dumps(report.as_dict(), indent=2) + "\n")
    return path


def orphaned(run_id: str, run_dir: Path, *, target_repo: Path | None = None,
             gate_ids: list[str] | None = None, gatectl_path: str | None = None) -> OrphanReport:
    """Reclaim every orphan of the run. Idempotent (1.3): a second pass finds
    nothing to reclaim. Never raises (2.5): every failure is recorded in the
    report and the caller continues.
    """
    report = OrphanReport()
    original_pid = control_pid(Path(run_dir))
    if pid_alive(original_pid):
        report.notes.append(f"run {run_id} is still live at pid {original_pid}; nothing reclaimed")
        return report
    try:
        reclaim_tmux(run_id, original_pid, report)
    except Exception as error:
        report.notes.append(f"tmux reclaim failed: {type(error).__name__}: {error}")
    try:
        reclaim_worktrees(target_repo, Path(run_dir), report)
    except Exception as error:
        report.notes.append(f"worktree reclaim failed: {type(error).__name__}: {error}")
    try:
        reclaim_gate_jobs(gate_ids or [], report, gatectl_path=gatectl_path)
    except Exception as error:
        report.notes.append(f"gate reclaim failed: {type(error).__name__}: {error}")
    return report


def _list_repo_worktrees(repo: Path) -> list[dict]:
    """Parse `git worktree list --porcelain` into entry dicts."""
    raw = wt.git(repo, "worktree", "list", "--porcelain", check=False)
    entries: list[dict] = []
    current: dict = {}
    for line in raw.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                entries.append(current)
                current = {}
            current["path"] = value.strip()
        elif key == "branch":
            current["branch"] = value.strip()
        elif key == "HEAD":
            current["head"] = value.strip()
        elif key in ("detached", "bare", "locked", "prunable"):
            current[key] = True
    if current:
        entries.append(current)
    return entries


def _control_run_id(run_dir: Path) -> str | None:
    path = run_dir / "control.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    rid = raw.get("run_id")
    return str(rid) if isinstance(rid, str) and rid.strip() else None


def reclaim_start_orphans(repo: Path | None, *, current_run_dir: Path | None = None,
                          runs_root: Path | None = None) -> OrphanReport:
    """Reclaim dead jbi worktrees, stale registrations, and orphan jbi/* branches at START.

    Never touches non-jbi worktrees, never touches worktrees or branches owned by a
    live jbi run or `current_run_dir`, and never touches `jbi-parked/*` refs. Never raises.
    """
    report = OrphanReport()
    if repo is None or not (Path(repo) / ".git").exists():
        return report
    repo = Path(repo)
    repo_real = os.path.realpath(repo)
    current_real = os.path.realpath(current_run_dir) if current_run_dir is not None else None
    runs_real = os.path.realpath(runs_root) if runs_root is not None else None
    live_branches: set[str] = set()
    try:
        main_branch = wt.current_branch_or_none(repo)
        if main_branch and main_branch.startswith("jbi/"):
            live_branches.add(main_branch)
    except Exception as error:
        report.notes.append(f"cannot read main branch: {type(error).__name__}: {error}")

    try:
        entries = _list_repo_worktrees(repo)
    except Exception as error:
        report.notes.append(f"cannot list worktrees: {type(error).__name__}: {error}")
        entries = []

    for entry in entries:
        raw_path = entry.get("path")
        if not raw_path:
            continue
        wt_path = Path(raw_path)
        wt_real = os.path.realpath(wt_path)
        if wt_real == repo_real:
            continue
        branch_ref = str(entry.get("branch") or "")
        branch_short = branch_ref.removeprefix("refs/heads/") if branch_ref.startswith("refs/heads/") else ""
        owner_run_dir = wt_path.parent.parent if wt_path.parent.name == "worktrees" else None
        under_runs = bool(runs_real and (wt_real == runs_real or wt_real.startswith(runs_real + os.sep)))
        has_run_meta = bool(owner_run_dir and (
            (owner_run_dir / "run.db").exists() or (owner_run_dir / "control.json").exists()))
        is_jbi = branch_short.startswith("jbi/") or under_runs or has_run_meta
        if not is_jbi:
            continue
        if owner_run_dir is not None:
            if current_real and os.path.realpath(owner_run_dir) == current_real:
                if branch_short:
                    live_branches.add(branch_short)
                continue
            owner_pid = control_pid(owner_run_dir)
            if pid_alive(owner_pid):
                report.worktrees_kept.append({"path": str(wt_path), "reason": f"live run pid {owner_pid}"})
                if branch_short:
                    live_branches.add(branch_short)
                continue
            dead_run_id = _control_run_id(owner_run_dir)
            if dead_run_id:
                try:
                    reclaim_tmux(dead_run_id, owner_pid, report)
                except Exception as error:
                    report.notes.append(f"tmux reclaim failed for {dead_run_id}: {error}")
        try:
            wt.remove_worktree(repo, wt_path)
            if wt_path.exists() and (under_runs or has_run_meta):
                shutil.rmtree(wt_path, ignore_errors=True)
                wt.git(repo, "worktree", "prune", check=False)
            if wt_path.exists():
                report.worktrees_kept.append({"path": str(wt_path), "reason": "worktree removal left files"})
                if branch_short:
                    live_branches.add(branch_short)
            else:
                report.worktrees_removed.append(str(wt_path))
        except Exception as error:
            report.worktrees_kept.append({"path": str(wt_path), "reason": f"{type(error).__name__}: {error}"})
            if branch_short:
                live_branches.add(branch_short)

    try:
        wt.git(repo, "worktree", "prune", check=False)
    except Exception as error:
        report.notes.append(f"git worktree prune failed: {error}")

    try:
        jbi_branches = [line.strip() for line in wt.git(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/jbi/", check=False).splitlines()
            if line.strip()]
    except Exception as error:
        report.notes.append(f"cannot list jbi/* branches: {error}")
        jbi_branches = []

    for branch in jbi_branches:
        if branch in live_branches:
            report.branches_kept.append({"branch": branch, "reason": "held by live worktree"})
            continue
        try:
            wt.git(repo, "branch", "-D", branch, check=False)
            if wt.head(repo, f"refs/heads/{branch}") is None:
                report.branches_removed.append(branch)
            else:
                report.branches_kept.append({"branch": branch, "reason": "git branch -D refused"})
        except Exception as error:
            report.branches_kept.append({"branch": branch, "reason": f"{type(error).__name__}: {error}"})

    return report
