"""Thin git helpers. Every call uses an argument list, never a shell.

The target repository keeps its base branch checked out (`main` for a new repository). Each task works in its own worktree on
branch `jbi/<task>`.
Host Git calls disable agent-controlled hooks, monitors, and external diff.
The configured CI service performs every merge. Host functions synchronize only its verified squash.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path
from typing import Iterable

MAIN = "main"
IDENTITY = ("-c", "user.name=just-build-it", "-c", "user.email=jbi@localhost")
HOST_CONFIG = ("--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "diff.external=", "-c", "protocol.file.allow=never")


def _host_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM") if k in os.environ}
    return {**env, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


_RUNTIME_GIT: dict[Path, tuple[Path, Path]] = {}


def pin_git_metadata(worktree: Path, repository: Path | None = None) -> tuple[Path, Path]:
    """Bind the registered identity of a task worktree before trust-dependent Git calls."""
    key = Path(worktree).resolve()
    if key not in _RUNTIME_GIT:
        _RUNTIME_GIT[key] = trusted_git_metadata(worktree, repository)
    return _RUNTIME_GIT[key]


def unpin_git_metadata(worktree: Path) -> None:
    _RUNTIME_GIT.pop(Path(worktree).resolve(), None)


class GitError(RuntimeError):
    """A git command failed. The message holds git's own error text."""


def trusted_git_metadata(workdir: Path, repository: Path | None = None) -> tuple[Path, Path]:
    """Pin trusted paths without Git. Repositories supply their trusted task registrations."""
    from claude_mux_lite.sandbox import discover_git
    path = Path(workdir).resolve()
    if repository is not None:
        root, common = discover_git(Path(repository).resolve())
        entry = path / ".git"
        if entry.is_dir():
            if root.resolve() != entry.resolve():
                raise GitError(f"the plain worktree {workdir} differs from repository {repository}")
            return root, common
        if not entry.is_file() or entry.is_symlink():
            raise GitError(f"cannot pin linked worktree {workdir}: its .git file is irregular")
        linked = (common / "worktrees").resolve()
        owner = linked / path.name
        record = owner / "gitdir"
        try:
            if owner.is_symlink() or not record.is_file() or record.is_symlink():
                raise GitError(f"no trusted registration for task worktree {workdir}")
            if owner.resolve().parent != linked or os.path.realpath(record.read_text().strip()) != str(entry):
                raise GitError(f"worktree registration mismatch for {workdir}")
        except OSError as exc:
            raise GitError(f"cannot inspect task worktree registrations: {exc}") from exc
        git_dir = owner.resolve()
        pointer = git_dir / "commondir"
        try:
            if pointer.read_text(encoding="utf-8").strip() != "../..":
                raise GitError(f"worktree {workdir} leaves repository metadata {repository}")
        except OSError as exc:
            raise GitError(f"cannot read the registered worktree commondir: {exc}") from exc
        current_common = common
        if not (git_dir / "HEAD").is_file():
            raise GitError(f"the registered worktree {workdir} has no HEAD")
        if current_common != common:
            raise GitError(f"worktree {workdir} resolves outside repository {repository}")
        return git_dir, current_common
    raise GitError("trusted Git storage requires the repository path")


def trusted_git(git_dir: Path, common_dir: Path, workdir: Path, *args: str,
                check: bool = True, result: bool = False) -> str | subprocess.CompletedProcess:
    """Run fixed host Git operations on pinned metadata and an empty attribute source."""
    from claude_mux_lite.harness import host_git_env
    if not common_dir.is_dir() or (common_dir / "objects" == git_dir / "objects" and common_dir != git_dir):
        raise GitError("pinned Git metadata does not name its shared repository")
    argv = ["git", *IDENTITY, "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false", "-c", "diff.external=", "-c", "protocol.file.allow=never",
            "-c", "core.attributesFile=/dev/null", "-c", "core.untrackedCache=false",
            "--attr-source=4b825dc642cb6eb9a060e54bf8d69288fbee4904",
            "-c", "commit.gpgSign=false", "-c", "extensions.worktreeConfig=false",
            "-c", "core.bare=false", *args]
    env = host_git_env()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env["GIT_COMMON_DIR"] = str(Path(common_dir).resolve())
    env["GIT_DIR"] = str(Path(git_dir).resolve())
    env["GIT_WORK_TREE"] = str(Path(workdir).resolve())
    proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)
    if result:
        return proc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    if "-z" in args:
        return proc.stdout
    return proc.stdout.strip()


def git(repo: Path | str, *args: str, check: bool = True) -> str:
    pinned = _RUNTIME_GIT.get(Path(repo).resolve())
    if pinned is not None:
        return trusted_git(*pinned, Path(repo).resolve(), *args, check=check)
    result = subprocess.run(["git", *IDENTITY, *HOST_CONFIG, "-C", str(repo), *args],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, env=_host_env())
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    if "-z" in args:
        return result.stdout
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


def fetch_branch(repo: Path, name: str, *, update_local: bool = False) -> str | None:
    """Refresh one branch from `origin`. Best effort: return its head, or None.

    The CI service merges on GitHub and fetches in its own lanes, so a squash commit
    it reports never exists in this repository until this host fetches the base.
    `update_local` also fast-forwards the local branch to `origin/<name>` when that
    branch is the one checked out here. A rewritten origin, a dirty target root, or
    a detached checkout never moves this host: the merge must fast-forward or it
    does not happen. A failed fetch returns None, never raises, so an offline host
    keeps its current base. The fetch alone re-enables the file protocol: this host
    fetches a remote its own owner configured, which is exactly the case
    `HOST_CONFIG` otherwise refuses.
    """
    argv = ["git", *IDENTITY, *HOST_CONFIG, "-c", "protocol.file.allow=always",
            "-C", str(repo), "fetch", "--quiet", "origin", name]
    result = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                            env=_host_env())
    if result.returncode != 0:
        return None
    if update_local and current_branch_or_none(repo) == name:
        origin_ref = f"refs/remotes/origin/{name}"
        merge = subprocess.run(["git", *IDENTITY, *HOST_CONFIG, "-C", str(repo),
                                "merge", "--quiet", "--ff-only", origin_ref],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL,
                               env=_host_env())
        if merge.returncode != 0:
            return head(repo, name)          # fetched, but the local branch stays put
    return head(repo, name)


def missing_branch(repo: Path, name: str) -> bool:
    """True when `name` names no branch under `refs/heads`.

    A tag, a short SHA, or HEAD is not a branch: `fast_forward_main` advances
    `refs/heads/<name>`, so only a real branch counts."""
    if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{name}", check=False):
        return False
    return name != git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False)


def ensure_branch(repo: Path, name: str, base: str) -> bool:
    """Create branch `name` at `base` when `name` names no branch. True when created."""
    if not missing_branch(repo, name):
        return False
    if head(repo, base) is None:
        raise GitError(f"cannot create branch {name}: base branch {base} has no commit")
    git(repo, "branch", name, base)
    return True


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """Check ancestry without changing the repository."""
    result = subprocess.run(["git", *IDENTITY, *HOST_CONFIG, "-C", str(repo), "merge-base",
                             "--is-ancestor", ancestor, descendant], capture_output=True,
                            stdin=subprocess.DEVNULL, env=_host_env())
    if result.returncode == 1:
        return False
    if result.returncode != 0:
        raise GitError(result.stderr.decode(errors="replace").strip())
    return True


def tree_of(repo: Path, sha: str) -> str:
    return git(repo, "rev-parse", f"{sha}^{{tree}}")


def commit_all(path: Path, message: str) -> str | None:
    """Stage everything and commit. Returns the new sha, or None when nothing changed."""
    git(path, "add", "-A")
    if not git(path, "status", "--porcelain"):
        return None
    git(path, "commit", "-q", "--no-verify", "-m", message)
    return git(path, "rev-parse", "HEAD")


def _path_text(value: str) -> str:
    """Remove only the explicit repository-relative prefix and one offered quote."""
    text = value.strip()
    if len(text) > 1 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text[2:] if text.startswith("./") else text


def dirty_paths(worktree: Path) -> list[str]:
    """Return every modified, added, deleted, renamed, or untracked path in `worktree`."""
    out = git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if not out:
        return []
    from claude_mux_lite.harness import parse_status
    paths: list[str] = []
    if not out.endswith("\0"):
        for raw in out.splitlines():
            parts = raw.split(None, 1)
            if len(parts) == 2:
                for item in (parts[1].split(" -> ") if " -> " in parts[1] else [parts[1]]):
                    cleaned = _path_text(item)
                    if cleaned and cleaned not in paths:
                        paths.append(cleaned)
        return paths
    for xy, path, origin in parse_status(out.encode("utf-8", "surrogateescape")):
        for item in (path, origin):
            if item and item not in paths:
                paths.append(item)
    return paths


def path_owned(path: str, owns: Iterable[str]) -> bool:
    """Return True when `path` matches at least one owned path, directory prefix, or glob."""
    normalized = _path_text(path)
    for raw in owns:
        pattern = _path_text(raw).rstrip("/")
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
        pin_git_metadata(path, repo)
        return path
    root.mkdir(parents=True, exist_ok=True)
    branch = f"jbi/{task}"
    if head(repo, branch):
        if (path / ".git").exists():
            git(path, "checkout", "-q", branch)
        else:
            git(repo, "worktree", "add", "-q", str(path), branch)
    elif (path / ".git").exists():
        git(path, "checkout", "-q", "-b", branch, base)
    else:
        git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)
    pin_git_metadata(path, Path(repo))
    return path


def restore_worktree(repo: Path, task: str, base: str, root: Path, expected_head: str | None = None) -> Path:
    """Attach a released task to its exact parked branch. Never overwrite existing work."""
    path = root / task
    if not (path / ".git").exists():
        unpin_git_metadata(path)
    else:
        pin_git_metadata(path, Path(repo))
    branch = f"jbi/{task}"
    if not (path / ".git").exists():
        existing = git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/jbi-parked/").splitlines()
        refs = [ref for ref in existing if ref.removeprefix("refs/heads/jbi-parked/").startswith(f"{task}-")]
        if not refs:
            return create_worktree(repo, task, base, root)
    if current_branch_or_none(path) == branch:
        return path
    if (expected_head is None and (path / ".git").exists() and current_branch_or_none(path) is None
            and head(path, "HEAD") == base and not dirty_paths(path)):
        return create_worktree(repo, task, base, root)
    refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/jbi-parked/").splitlines()
    refs = [ref for ref in refs if ref.removeprefix("refs/heads/jbi-parked/").startswith(f"{task}-")]
    matches = [ref for ref in refs if expected_head is None or head(repo, ref) == expected_head]
    if len(matches) != 1:
        raise GitError(f"task {task} has {len(matches)} parked refs matching its recorded head")
    if (path / ".git").exists() and dirty_paths(path):
        raise GitError(f"task {task} has dirty or untracked work; parked-checkout refused")
    if not (path / ".git").exists():
        git(repo, "worktree", "prune", check=False)
    existing = head(repo, branch)
    if existing and existing != head(repo, matches[0]):
        raise GitError(f"task {task} already has a different branch {branch}")
    if not (path / ".git").exists():
        if path.exists():
            raise GitError(f"task {task} has files but no worktree metadata")
        git(repo, "worktree", "add", "-q", "--detach", str(path), matches[0])
    if matches[0] != branch:
        git(repo, "branch", "-m", matches[0].removeprefix("refs/heads/"), branch)
    git(path, "checkout", branch)
    pin_git_metadata(path, Path(repo))
    return path


def current_branch_or_none(repo: Path) -> str | None:
    return git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False) or None


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
    unpin_git_metadata(path)
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


def link_ignored_deps(repo: Path, worktree: Path) -> list[str]:
    """Create gitignored `node_modules/` directories in `worktree` populated with symlinks to `repo`.

    Git pattern `node_modules/` (trailing slash) ignores real directories, not
    top-level symlinks. Creating a real directory whose immediate children are
    symlinks into `repo/<rel>/node_modules/*` keeps `dirty_paths` and `commit_all`
    completely clean while exposing all packages and `.bin` executables read-only
    inside the bwrap root mount.
    """
    linked: list[str] = []
    repo_root = Path(repo).resolve()
    wt_root = Path(worktree).resolve()
    if repo_root == wt_root or not repo_root.is_dir() or not wt_root.is_dir():
        return linked
    skip_dirs = {".git", "worktrees", ".next", "dist", "build", "coverage", ".turbo", "__pycache__"}
    candidates: list[Path] = []
    for base, dirs, _files in os.walk(repo_root, followlinks=False):
        rel_base = Path(base).relative_to(repo_root)
        depth = len(rel_base.parts)
        if "node_modules" in dirs:
            candidates.append(rel_base / "node_modules" if depth > 0 else Path("node_modules"))
            dirs.remove("node_modules")
        if depth >= 3:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
    for rel in sorted(candidates):
        src = repo_root / rel
        dst = wt_root / rel
        if not src.is_dir() or not dst.parent.is_dir() or os.path.lexists(dst):
            continue
        chk = subprocess.run(["git", *HOST_CONFIG, "-C", str(wt_root), "check-ignore", "-q", rel.as_posix() + "/"],
                             capture_output=True, stdin=subprocess.DEVNULL, env=_host_env())
        if chk.returncode != 0:
            continue
        try:
            dst.mkdir(exist_ok=True)
            for entry in os.scandir(src):
                if entry.name in (".vite", ".vite-temp", ".cache"):
                    continue
                link_path = dst / entry.name
                if os.path.lexists(link_path):
                    continue
                target_path = Path(entry.path)
                if entry.is_symlink():
                    try:
                        real_target = Path(os.path.realpath(entry.path))
                        rel_target = real_target.relative_to(repo_root)
                        wt_pkg = wt_root / rel_target
                        if wt_pkg.is_dir():
                            target_path = wt_pkg
                            src_dist = real_target / "dist"
                            dst_dist = wt_pkg / "dist"
                            if src_dist.is_dir() and not os.path.lexists(dst_dist):
                                dchk = subprocess.run(
                                    ["git", *HOST_CONFIG, "-C", str(wt_root), "check-ignore", "-q",
                                     (rel_target / "dist").as_posix() + "/"],
                                    capture_output=True, stdin=subprocess.DEVNULL, env=_host_env())
                                if dchk.returncode == 0:
                                    dst_dist.mkdir(exist_ok=True)
                                    for dentry in os.scandir(src_dist):
                                        os.symlink(dentry.path, dst_dist / dentry.name)
                    except (OSError, ValueError):
                        pass
                os.symlink(target_path, link_path)
            linked.append(rel.as_posix())
        except OSError:
            pass
    return linked


def prepare_worktree(worktree: Path, *, repo: Path | None = None, setup_commands: Iterable[str] = (),
                     cache_key: str | None = None, timeout: int = 300) -> list[str]:
    """Purge stale `.tsbuildinfo` caches, link gitignored `node_modules`, and run `setup_commands`."""
    notes: list[str] = []
    if repo is not None:
        linked = link_ignored_deps(repo, worktree)
        if linked:
            notes.append(f"linked {len(linked)} node_modules dir(s)")
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
