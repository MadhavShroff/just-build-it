"""A Linux shell fence. Namespace mounts expose only authorized writable directories.

The fence needs `bwrap`. It allows no live shell fallback when the executable is absent.

Derived from the harness of the MIT-licensed research project.
The full notice is in licenses/RSWARM-MIT.txt.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Files in the Git folder of the task that the fenced shell may write: the index, the merge and
# commit state, the objects and the one leased branch. The brief lists them by name.
STATE_FILES = ("index", "MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "COMMIT_EDITMSG", "ORIG_HEAD")
# Files in the per-worktree Git folder that steer the host Git. They stay read-only.
STEERING_FILES = ("gitdir", "commondir", "config.worktree", "config", "hooks", "info", "packed-refs")


def discover_git(workdir: Path) -> tuple[Path, Path]:
    """Return (git_dir, common_dir) by reading the `.git` entry. No Git program runs.

    Call this before an agent runs. The agent can rewrite the `.git` file afterward."""
    entry = Path(os.path.realpath(workdir)) / ".git"
    if entry.is_dir():
        return entry, entry
    text = entry.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        raise SandboxError("the .git entry is not a gitdir file")
    git_dir = Path(os.path.realpath(entry.parent / text.split(":", 1)[1].strip()))
    common = git_dir
    pointer = git_dir / "commondir"
    if pointer.is_file():
        common = Path(os.path.realpath(git_dir / pointer.read_text(encoding="utf-8").strip()))
    return git_dir, common


def _branch_ref(git_dir: Path) -> str | None:
    """The leased branch ref, such as refs/heads/jbi/t1. None on a detached HEAD."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return head.split(":", 1)[1].strip() if head.startswith("ref:") else None


class SandboxError(RuntimeError):
    """The required operating-system fence cannot run."""


class LinuxSandbox:
    """Expose the worktree, one leased branch and an own scratch directory as writable locations."""

    def __init__(self, workdir: Path, *, executable: str | None = None, scratch: Path | None = None,
                 git_dir: Path | None = None, common_dir: Path | None = None):
        self.workdir = workdir.resolve()
        self.executable = executable or shutil.which("bwrap")
        self.scratch = scratch
        self.root: Path | None = None
        self.git_dir, self.common_dir = git_dir, common_dir
        if not self.executable:
            raise SandboxError("bwrap is required for live bash dispatch")
        if not (self.workdir / ".git").exists():
            raise SandboxError("bash dispatch needs a git worktree")

    def __enter__(self) -> "LinuxSandbox":
        if self.root is not None:
            return self
        scratch_root = self.scratch.parent if self.scratch else None
        if scratch_root is not None:
            scratch_root.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="jbi-lite-sandbox-", dir=scratch_root))
        if self.git_dir is None or self.common_dir is None:
            self.git_dir, self.common_dir = discover_git(self.workdir)
        scratch = Path(self.scratch) if self.scratch else self.root / "scratch"
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(scratch, 0o700)
        self.scratch = scratch
        return self

    def __exit__(self, *_: object) -> None:
        if self.scratch:
            subprocess.run(["chmod", "--recursive", "u+w", str(self.scratch)], capture_output=True)
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)
        self.root = None

    # ---- Git storage mounts ----------------------------------------------------------------

    @staticmethod
    def _ro(path: Path) -> list[str]:
        return ["--ro-bind", str(path), str(path)]

    @staticmethod
    def _rw(path: Path) -> list[str]:
        return ["--bind", str(path), str(path)]

    def _seal_siblings(self, folder: Path, keep: Path | None) -> list[str]:
        """Read-only mounts of every entry in `folder` except `keep`. A mount cannot be unlinked."""
        out: list[str] = []
        for entry in sorted(folder.iterdir()):
            if keep is not None and entry == keep:
                continue
            out += self._ro(entry)
        return out

    def git_mounts(self) -> list[str]:
        """Mounts for Git storage. The remaining storage stays read-only.

        A linked worktree has its own folder, which is writable except the files that steer Git.
        Common storage opens the objects and the one leased branch with its log. A second task
        branch stays read-only. Every item mounts only when it exists, with its own type."""
        git_dir, common = self.git_dir, self.common_dir
        assert git_dir is not None and common is not None
        linked = git_dir != common
        ref = _branch_ref(git_dir)
        args: list[str] = []
        if linked:
            args += self._ro(common)
            args += self._rw(git_dir)
            for name in STEERING_FILES:
                if (git_dir / name).exists():
                    args += self._ro(git_dir / name)
            head = git_dir / "HEAD"
            if ref and head.is_file():
                args += self._ro(head)
        else:
            args += self._rw(git_dir)
            for entry in sorted(git_dir.iterdir()):
                if entry.name in STATE_FILES or (entry.name == "refs" and ref is not None):
                    continue
                args += self._ro(entry)
        args += self._rw(common / "objects") if (common / "objects").is_dir() else []
        root_refs = common / "refs"
        if root_refs.is_dir() and ref is None:
            args += self._ro(root_refs)
        if ref and ref.startswith("refs/"):
            leaf = common / ref
            args += self._rw(root_refs) + self._seal_siblings(root_refs, root_refs / "heads")
            lead = common / "refs/heads"
            lead.mkdir(parents=True, exist_ok=True)
            leaf.parent.mkdir(parents=True, exist_ok=True)
            args += self._rw(lead)
            args += self._seal_siblings(lead, leaf.parent if leaf.parent != lead else leaf)
            if leaf.parent != lead:
                args += self._rw(leaf.parent)
                # The leased ref file remains unmounted. Git replaces it by rename.
                args += self._seal_siblings(leaf.parent, leaf)
        if ref:
            logs = common / "logs"
            if logs.is_dir():
                args += self._ro(logs)
            leaf = common / "logs" / ref
            parent = leaf.parent
            if parent.is_dir():
                args += self._rw(parent) + self._seal_siblings(parent, leaf)
            elif leaf.parent.parent.is_dir():
                args += self._rw(leaf.parent.parent) + self._seal_siblings(leaf.parent.parent, parent)
            reflog = git_dir / "logs" / "HEAD"
            if reflog.exists():
                args += self._rw(reflog)
        return args

    def argv(self, command: str) -> list[str]:
        """Wrap one shell in a read-only root, a private /tmp and the authorized writable leaves."""
        if self.root is None:
            raise SandboxError("the sandbox must be entered before bash dispatch")
        argv = [self.executable, "--unshare-all", "--die-with-parent", "--new-session",
                "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
                "--tmpfs", "/tmp",
                "--bind", str(self.workdir), str(self.workdir),
                *self.git_mounts(),
                "--bind", str(self.scratch), str(self.scratch),
                "--setenv", "JBI_WORKDIR", str(self.workdir), "--setenv", "TMPDIR", str(self.scratch),
                "--", "bash", "-c", 'cd "$JBI_WORKDIR" && ' + command]
        return argv


def shell_command(workdir: Path, command: str, *, scratch: Path | None = None) -> tuple[list[str], LinuxSandbox]:
    """Return a fenced shell argv and its owning context. The caller must close the context."""
    fence = LinuxSandbox(workdir, scratch=Path(scratch) if scratch else None)
    fence.__enter__()
    return fence.argv(command), fence
