"""A Linux shell fence. Namespace mounts expose only authorized writable directories.

The fence needs `bwrap`. It allows no live shell fallback when the executable is absent.
Git refs and configuration remain read-only. The parent publishes a validated private commit.

Derived from the harness of the MIT-licensed research project.
The full notice is in licenses/RSWARM-MIT.txt.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


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


class SandboxError(RuntimeError):
    """The required operating-system fence cannot run."""


class LinuxSandbox:
    """Expose the worktree, one private Git repository and an own scratch directory."""

    def __init__(self, workdir: Path, *, executable: str | None = None, scratch: Path | None = None,
                 git_dir: Path | None = None, common_dir: Path | None = None,
                 private_git: bool = False, real_common_dir: Path | None = None):
        self.workdir = workdir.resolve()
        self.executable = executable or shutil.which("bwrap")
        self.scratch = scratch
        self.root: Path | None = None
        self.git_dir, self.common_dir = git_dir, common_dir
        self.private_git = private_git
        self.real_common_dir = real_common_dir
        self.no_hooks: Path | None = None
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
        self.no_hooks = self.root / "no-hooks"
        self.no_hooks.mkdir(mode=0o700)
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

    def git_mounts(self) -> list[str]:
        """Make all Git storage read-only. The parent uses no pointer controlled by the shell."""
        git_dir, common = self.git_dir, self.common_dir
        assert git_dir is not None and common is not None
        args = (["--bind", str(common), str(common)] if self.private_git else self._ro(common))
        if git_dir != common:
            args += (["--bind", str(git_dir), str(git_dir)] if self.private_git else self._ro(git_dir))
            entry = self.workdir / ".git"
            if entry.is_file():
                args += self._ro(entry)
        if self.private_git and self.real_common_dir is not None:
            args += self._ro(Path(self.real_common_dir))
        return args

    def argv(self, command: str) -> list[str]:
        """Wrap one shell in a read-only root, a private /tmp and the authorized writable leaves."""
        if self.root is None:
            raise SandboxError("the sandbox must be entered before bash dispatch")
        argv = [self.executable, "--unshare-all", "--die-with-parent", "--new-session",
                "--clearenv", "--setenv", "PATH", os.environ.get("PATH", "/usr/bin:/bin"),
                "--setenv", "HOME", os.environ.get("HOME", str(self.workdir)),
                "--setenv", "USER", os.environ.get("USER", "jbi"),
                "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
                "--tmpfs", "/tmp",
                "--bind", str(self.workdir), str(self.workdir),
                *self.git_mounts(),
                "--bind", str(self.scratch), str(self.scratch),
                "--ro-bind", str(self.no_hooks), str(self.no_hooks),
                "--setenv", "JBI_WORKDIR", str(self.workdir), "--setenv", "TMPDIR", str(self.scratch)]
        if self.private_git:
            argv += ["--setenv", "GIT_DIR", str(self.git_dir),
                     "--setenv", "GIT_COMMON_DIR", str(self.common_dir),
                     "--setenv", "GIT_WORK_TREE", str(self.workdir),
                     "--setenv", "GIT_CONFIG_COUNT", "6",
                     "--setenv", "GIT_CONFIG_KEY_0", "commit.gpgSign",
                     "--setenv", "GIT_CONFIG_VALUE_0", "false",
                     "--setenv", "GIT_CONFIG_KEY_1", "core.hooksPath",
                     "--setenv", "GIT_CONFIG_VALUE_1", str(self.no_hooks),
                     "--setenv", "GIT_CONFIG_KEY_2", "core.fsmonitor",
                     "--setenv", "GIT_CONFIG_VALUE_2", "false",
                     "--setenv", "GIT_CONFIG_KEY_3", "tag.gpgSign",
                     "--setenv", "GIT_CONFIG_VALUE_3", "false",
                     "--setenv", "GIT_CONFIG_KEY_4", "core.untrackedCache",
                     "--setenv", "GIT_CONFIG_VALUE_4", "false",
                     "--setenv", "GIT_CONFIG_KEY_5", "diff.external",
                     "--setenv", "GIT_CONFIG_VALUE_5", ""]
        argv += ["--", "bash", "-c", 'cd "$JBI_WORKDIR" && ' + command]
        return argv


def fenced_argv(guard: object, command: str, private: object, scratch: Path) -> tuple[list[str], LinuxSandbox]:
    """Return the production private-Git shell invocation. The caller owns process timing."""
    refusal = guard.check(command)
    if refusal:
        raise ValueError("command refused by the fence: " + refusal)
    fence = LinuxSandbox(guard.root, git_dir=guard.git_dir, common_dir=guard.common_dir,
                         scratch=scratch / "command", private_git=True,
                         real_common_dir=private.real_common)
    fence.__enter__()
    return fence.argv(command), fence


def fenced_bash(guard: object, command: str, private: object, scratch: Path, *,
                timeout: float = 120) -> subprocess.CompletedProcess:
    """A focused direct-shell check uses the same private Git mechanism as the harness."""
    argv, fence = fenced_argv(guard, command, private, scratch)
    from .harness import ToolError, fence_env
    with fence:
        result = subprocess.run(argv, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, env=fence_env(), timeout=timeout)
    private.checkpoint()
    reverted, errors = guard.reconcile()
    if errors or reverted:
        private.checkpoint_error = "the command changed an unowned path; private commits are refused"
    if result.returncode != 0:
        private.checkpoint_error = "a failed command invalidated the private Git transaction"
    if reverted or errors:
        result.stderr += "\n" + "; ".join([f"reverted {', '.join(reverted)}"] if reverted else [] + errors)
        result.returncode = result.returncode or 1
    return result


def leased_shell(guard: object, command: str) -> subprocess.CompletedProcess:
    """Run a complete direct-shell transaction. Test fixtures use this boundary."""
    from .private_git import PrivateGit
    scratch = Path(tempfile.mkdtemp(prefix="jbi-shell-test-"))
    private = PrivateGit(guard, scratch)
    try:
        result = fenced_bash(guard, command, private, scratch)
        private.publish()
        return result
    except (ValueError, RuntimeError) as error:
        return subprocess.CompletedProcess(command, 1, "", str(error))
    finally:
        private.__exit__()
        shutil.rmtree(scratch, ignore_errors=True)


def shell_command(workdir: Path, command: str, *, scratch: Path | None = None) -> tuple[list[str], LinuxSandbox]:
    """Return a fenced shell argv and its owning context. The caller must close the context."""
    fence = LinuxSandbox(workdir, scratch=Path(scratch) if scratch else None)
    fence.__enter__()
    return fence.argv(command), fence
