"""Thin git helpers. Every call uses an argument list, never a shell.

The target repository keeps its base branch checked out (`main` for a new repository). Each task works in its own worktree on
branch `jbi/<task>`. A lane squash-merges a task branch onto a pinned base in a
temporary detached worktree, then fast-forwards `main` to the squash commit.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path
from typing import Iterable

MAIN = "main"
IDENTITY = ("-c", "user.name=just-build-it", "-c", "user.email=jbi@localhost")


class GitError(RuntimeError):
    """A git command failed. The message holds git's own error text."""


def git(repo: Path | str, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *IDENTITY, "-C", str(repo), *args],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def init_repo(path: Path) -> str:
    """Create `path` as a repository on `main` if it is not one yet. Returns its current branch.

    An existing repository keeps its own branch. A detached HEAD raises GitError."""
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        git(path, "init", "-q", "-b", MAIN)
    return current_branch(path)


def current_branch(repo: Path) -> str:
    """The branch that HEAD names. Raises GitError on a detached HEAD."""
    out = git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    if not out:
        raise GitError(f"{repo} has a detached HEAD. Check out a branch, then retry bootstrap.")
    return out


def head(repo: Path, ref: str = MAIN) -> str | None:
    """The commit that `ref` names, or None when the ref does not exist yet."""
    out = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
    return out or None


def tree_of(repo: Path, sha: str) -> str:
    return git(repo, "rev-parse", f"{sha}^{{tree}}")


def commit_all(path: Path, message: str) -> str | None:
    """Stage everything and commit. Returns the new sha, or None when nothing changed."""
    git(path, "add", "-A")
    if not git(path, "status", "--porcelain"):
        return None
    git(path, "commit", "-q", "--no-verify", "-m", message)
    return git(path, "rev-parse", "HEAD")


def dirty_paths(worktree: Path) -> list[str]:
    """Return every modified, added, deleted, renamed, or untracked path in `worktree`."""
    out = git(worktree, "status", "--porcelain", "-uall")
    if not out:
        return []
    paths: list[str] = []
    for raw in out.splitlines():
        parts = raw.lstrip().split(None, 1)
        if len(parts) < 2:
            continue
        rest = parts[1].strip()
        for item in (rest.split(" -> ") if " -> " in rest else [rest]):
            clean = item.strip().strip('"').lstrip("./")
            if clean and clean not in paths:
                paths.append(clean)
    return paths


def path_owned(path: str, owns: Iterable[str]) -> bool:
    """Return True when `path` matches at least one owned path, directory prefix, or glob."""
    normalized = path.strip().lstrip("./")
    for raw in owns:
        pattern = raw.strip().lstrip("./").rstrip("/")
        if not pattern:
            continue
        if normalized == pattern or normalized.startswith(pattern + "/"):
            return True
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if not prefix or normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(normalized, pattern):
            return True
    return False


def create_worktree(repo: Path, task: str, base: str, root: Path) -> Path:
    """A worktree for `task` on branch jbi/<task>. An existing worktree is reused."""
    path = root / task
    if (path / ".git").exists():
        return path
    root.mkdir(parents=True, exist_ok=True)
    branch = f"jbi/{task}"
    if head(repo, branch):
        git(repo, "worktree", "add", "-q", str(path), branch)
    else:
        git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)
    return path


def discard_changes(path: Path) -> None:
    """Drop uncommitted edits, for example from a verifier that must not edit code."""
    git(path, "reset", "-q", "--hard", "HEAD")
    git(path, "clean", "-q", "-fd")


def squash_merge_tree(repo: Path, base: str, branch: str, tmp: Path) -> str:
    """Squash-merge `branch` onto `base` in a fresh detached worktree at `tmp`.

    Returns the tree of the combined result. Raises GitError on a conflict."""
    remove_worktree(repo, tmp)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "--detach", str(tmp), base)
    merged = subprocess.run(["git", *IDENTITY, "-C", str(tmp), "merge", "-q", "--squash", branch],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if merged.returncode != 0:
        raise GitError(f"squash merge of {branch} onto {base[:10]} conflicts: "
                       f"{(merged.stdout + merged.stderr).strip()}")
    git(tmp, "add", "-A")
    return git(tmp, "write-tree")


def commit_tree(repo: Path, tree: str, parent: str, message: str) -> str:
    """Create a commit object for `tree` on `parent` without touching any checkout."""
    return git(repo, "commit-tree", tree, "-p", parent, "-m", message)


def fast_forward_main(repo: Path, sha: str, expected: str, branch: str = MAIN) -> bool:
    """Move `branch` from `expected` to `sha`. Refuses when it is no longer `expected`."""
    if head(repo, branch) != expected:
        return False
    checked_out = git(repo, "symbolic-ref", "-q", "HEAD", check=False) == f"refs/heads/{branch}"
    if checked_out:
        git(repo, "merge", "-q", "--ff-only", sha)
    else:
        git(repo, "update-ref", f"refs/heads/{branch}", sha, expected)
    return head(repo, branch) == sha


def remove_worktree(repo: Path, path: Path) -> None:
    if path.exists():
        git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "worktree", "prune", check=False)


def purge_tsbuildinfo(worktree: Path) -> list[str]:
    """Delete stale `*.tsbuildinfo` files under `worktree` so `tsc` never gives a false green."""
    removed: list[str] = []
    if not worktree.is_dir():
        return removed
    for root, dirs, files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules")]
        for name in files:
            if name.endswith(".tsbuildinfo"):
                target = Path(root) / name
                try:
                    target.unlink()
                    removed.append(str(target.relative_to(worktree)))
                except OSError:
                    pass
    return sorted(removed)


def prepare_worktree(worktree: Path, *, setup_commands: Iterable[str] = (),
                     cache_key: str | None = None, timeout: int = 300) -> list[str]:
    """Purge stale `.tsbuildinfo` caches and run workspace `setup_commands` when `cache_key` changed."""
    notes: list[str] = []
    purged = purge_tsbuildinfo(worktree)
    if purged:
        notes.append(f"purged {len(purged)} tsbuildinfo file(s)")
    commands = [cmd.strip() for cmd in setup_commands if isinstance(cmd, str) and cmd.strip()]
    if not commands:
        return notes
    marker = worktree / ".jbi" / ".prepared-key"
    if cache_key and marker.is_file():
        try:
            if marker.read_text(encoding="utf-8").strip() == cache_key:
                notes.append(f"setup cache hit ({cache_key[:12]})")
                return notes
        except OSError:
            pass
    for cmd in commands:
        proc = subprocess.run(cmd, shell=True, cwd=str(worktree), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[-400:]
            raise GitError(f"worktree setup command failed (rc={proc.returncode}): {cmd}: {detail}")
        notes.append(f"ran setup: {cmd}")
    if cache_key:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(cache_key + "\n", encoding="utf-8")
        except OSError:
            pass
    return notes


def host_admission_check(*, max_load_per_cpu: float = 0.0, min_free_mem_mb: int = 0) -> str | None:
    """Return None when host CPU load and available memory allow a new job, else a refusal reason."""
    if max_load_per_cpu > 0:
        try:
            load1 = os.getloadavg()[0]
            cpus = max(1, os.cpu_count() or 1)
            ratio = load1 / cpus
            if ratio > max_load_per_cpu:
                return f"host load {load1:.2f} across {cpus} CPUs ({ratio:.2f}/cpu > {max_load_per_cpu:.2f})"
        except OSError:
            pass
    if min_free_mem_mb > 0:
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    avail_mb = int(line.split()[1]) // 1024
                    if avail_mb < min_free_mem_mb:
                        return f"host MemAvailable {avail_mb} MiB < {min_free_mem_mb} MiB"
                    break
        except (OSError, ValueError, IndexError):
            pass
    return None
