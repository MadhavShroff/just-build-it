"""Thin git helpers. Every call uses an argument list, never a shell.

The target repository keeps its base branch checked out (`main` for a new repository). Each task works in its own worktree on
branch `jbi/<task>`. A lane squash-merges a task branch onto a pinned base in a
temporary detached worktree, then fast-forwards `main` to the squash commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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
