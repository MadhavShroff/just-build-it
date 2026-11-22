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


class SandboxError(RuntimeError):
    """The required operating-system fence cannot run."""


class LinuxSandbox:
    """Expose the Git folders and an own scratch directory as writable locations."""

    def __init__(self, workdir: Path, *, executable: str | None = None, scratch: Path | None = None):
        self.workdir = workdir.resolve()
        self.executable = executable or shutil.which("bwrap")
        self.scratch = scratch
        self.root: Path | None = None
        if not self.executable:
            raise SandboxError("bwrap is required for live bash dispatch")
        if not (self.workdir / ".git").exists():
            raise SandboxError("bash dispatch needs a git worktree")

    @staticmethod
    def _git(workdir: Path, *arguments: str) -> str:
        answer = subprocess.run(["git", "-C", str(workdir), *arguments], capture_output=True,
                                text=True, stdin=subprocess.DEVNULL, timeout=30)
        if answer.returncode:
            raise SandboxError(answer.stderr.strip() or "cannot resolve Git folders")
        return answer.stdout.strip()

    def __enter__(self) -> "LinuxSandbox":
        if self.root is not None:
            return self
        scratch_root = self.scratch.parent if self.scratch else None
        if scratch_root is not None:
            scratch_root.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="jbi-lite-sandbox-", dir=scratch_root))
        worktree = self._git(self.workdir, "rev-parse", "--show-toplevel")
        common = self._git(self.workdir, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.workdir = Path(worktree).resolve()
        git_common = Path(common).resolve()
        # Only common Git storage and the task worktree are writable.
        common_parent = git_common
        scratch = Path(self.scratch) if self.scratch else self.root / "scratch"
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(scratch, 0o700)
        self.common_parent, self.scratch = common_parent, scratch
        return self

    def __exit__(self, *_: object) -> None:
        if self.scratch:
            subprocess.run(["chmod", "--recursive", "u+w", str(self.scratch)], capture_output=True)
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)
        self.root = None

    def argv(self, command: str) -> list[str]:
        """Wrap one shell in a read-only filesystem with two authorized writable leaves."""
        if self.root is None:
            raise SandboxError("the sandbox must be entered before bash dispatch")
        # git-common-dir can be a directory itself, but its .name here holds the common root
        git_dir = self.common_parent
        argv = [self.executable, "--unshare-all", "--die-with-parent", "--new-session",
                "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
                "--tmpfs", "/tmp",
                "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
                "--bind", str(self.workdir), str(self.workdir),
                "--bind", str(git_dir), str(git_dir),
                "--bind", str(self.scratch), str(self.scratch),
                "--setenv", "JBI_WORKDIR", str(self.workdir), "--setenv", "TMPDIR", str(self.scratch),
                "--", "bash", "-c", 'cd "$JBI_WORKDIR" && ' + command]
        return argv


def shell_command(workdir: Path, command: str, *, scratch: Path | None = None) -> tuple[list[str], LinuxSandbox]:
    """Return a fenced shell argv and its owning context. The caller must close the context."""
    fence = LinuxSandbox(workdir, scratch=Path(scratch) if scratch else None)
    fence.__enter__()
    return fence.argv(command), fence
