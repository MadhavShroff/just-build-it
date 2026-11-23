"""Private Git metadata for the lifetime of one harness task.

Git state mutation stays in a private directory until one task transaction finishes. The parent publishes
only commit objects after lease and ancestry checks. No agent-controlled Git path reaches a host
programmatic driver. The real worktree is the explicit Git work-tree throughout the command.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .harness import BashGuard

OID = re.compile(r"[0-9a-f]{40,64}\Z")


class PrivateGit:
    """One isolated Git directory and a host transaction for its resulting leased-branch commits."""

    def __init__(self, guard: "BashGuard", scratch: Path):
        self.guard = guard
        self.scratch = scratch
        self.root = Path(mkdtemp(prefix="jbi-private-git-", dir=scratch))
        self.git_dir = self.root / "git"
        self.common_dir = self.git_dir
        self.git_dir.mkdir(mode=0o700)
        self.real_git, self.real_common = guard.git_dir, guard.common_dir
        self.before = guard._rev()
        self.checkpoint_error = ""
        try:
            self._initialize()
        except Exception:
            shutil.rmtree(self.root, ignore_errors=True)
            raise

    def _initialize(self) -> None:
        if not OID.fullmatch(self.before) or not self.guard.branch.startswith("refs/heads/"):
            raise RuntimeError("private Git needs one full commit and one attached branch")
        for name in ("HEAD", "index"):
            source = self.real_git / name
            if source.is_symlink() or not source.is_file():
                raise RuntimeError("private Git requires regular HEAD and index files")
            shutil.copyfile(source, self.git_dir / name)
        for name in ("refs", "logs"):
            source = self.real_common / name
            if source.is_dir():
                shutil.copytree(source, self.git_dir / name, symlinks=False)
        pack = self.real_common / "packed-refs"
        if pack.is_symlink() or pack.exists() and not pack.is_file():
            raise RuntimeError("private Git requires a regular packed-refs file")
        if pack.is_file():
            shutil.copyfile(pack, self.git_dir / "packed-refs")
        if (self.real_common / "objects" / "info" / "alternates").exists():
            raise RuntimeError("alternating object stores are outside the private task transaction")
        (self.git_dir / "objects" / "info").mkdir(parents=True)
        (self.git_dir / "objects" / "pack").mkdir()
        # A real, private object store prevents shell-created objects from entering the real repo.
        (self.git_dir / "objects" / "info" / "alternates").write_text(
            os.path.realpath(self.real_common / "objects") + "\n", encoding="utf-8")
        (self.git_dir / "config").write_text(
            "[core]\n repositoryformatversion = 0\n bare = false\n worktree = "
            + str(self.guard.root) + "\n[user]\n name = jbi task\n email = task@local.invalid\n", encoding="ascii")
        from .harness import host_git_argv, host_git_env
        result = subprocess.run(host_git_argv("status", "--porcelain=v1", "-z",
                                             work_tree=self.guard.root, git_dir=self.git_dir),
                                env=host_git_env(), cwd=str(self.guard.root), capture_output=True, stdin=subprocess.DEVNULL,
                                timeout=120)
        if result.returncode != 0:
            raise RuntimeError("private Git metadata could not read the pinned worktree")
        self.guard.git_dir, self.guard.common_dir = self.git_dir, self.common_dir
        # Force the real worktree before any Git discovery.
        set_tree = subprocess.run(host_git_argv("config", "--local", "core.worktree", str(self.guard.root),
                                               git_dir=self.git_dir, work_tree=self.guard.root),
                                  env=host_git_env(), capture_output=True, stdin=subprocess.DEVNULL)
        if set_tree.returncode != 0:
            shutil.rmtree(self.root, ignore_errors=True)
            raise RuntimeError("private Git could not pin its worktree")

    def __enter__(self) -> "PrivateGit":
        return self

    def __exit__(self, *_: object) -> None:
        self.guard.git_dir, self.guard.common_dir = self.real_git, self.real_common
        subprocess.run(["chmod", "--recursive", "u+w", str(self.root)], capture_output=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def _objects(self, sha: str) -> bool:
        from .harness import host_git_argv, host_git_env
        proc = subprocess.run(host_git_argv("cat-file", "-e", sha + "^{commit}",
                                           git_dir=self.real_git, work_tree=self.guard.root),
                              env=host_git_env(), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=60)
        return proc.returncode == 0

    def checkpoint(self) -> None:
        """Keep an ordered checkpoint of a private commit before checking ownership."""
        from .harness import host_git_argv, host_git_env
        if self.guard.branch != self.guard._git("symbolic-ref", "-q", "HEAD")[1].decode().strip():
            self.checkpoint_error = "the private command switched the leased branch"
        head = self.guard._rev()
        if not OID.fullmatch(head) or self._objects(head):
            return
        if self.guard.allow_commits:
            self.checkpoint_error = ""
        else:
            self.checkpoint_error = "a shell command made an unauthorized branch commit"

    def publish(self) -> tuple[str, list[str]]:
        """Validate and copy new commit ancestry. Return the new SHA or an empty SHA.

        The caller holds the task lease for every path check and the final ref update."""
        if self.checkpoint_error:
            raise RuntimeError(self.checkpoint_error)
        if not OID.fullmatch(self.before):
            raise RuntimeError("the pinned base is not a full commit id")
        head = self.guard._rev()
        if head == self.before:
            return "", []
        for xy, rel, original in self.guard._status():
            if (xy[1] in "ADMU" or xy[:1] in "ADMU") and rel in self.guard.baseline:
                raise RuntimeError("changes to initially dirty tracked files do not publish a branch")
        if not OID.fullmatch(head):
            raise RuntimeError("the private Git transaction produced no commit")
        if self.guard.branch != self.guard._git("symbolic-ref", "-q", "HEAD")[1].decode().strip():
            raise RuntimeError("the real worktree branch changed during the command")
        from .harness import host_git_argv, host_git_env
        proc = subprocess.run(host_git_argv("rev-list", "--reverse", self.before + ".." + head,
                                           git_dir=self.git_dir, work_tree=self.guard.root),
                              env=host_git_env(), capture_output=True, timeout=120)
        if proc.returncode != 0 or self._objects(head):
            return "", []
        commits = proc.stdout.decode("ascii").splitlines()
        if not commits or not all(OID.fullmatch(sha) for sha in commits):
            raise RuntimeError("private commit ancestry is invalid")
        ancestry = subprocess.run(host_git_argv("merge-base", "--is-ancestor", self.before, head,
                                               git_dir=self.git_dir, work_tree=self.guard.root),
                                  env=host_git_env(), stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=60)
        if ancestry.returncode != 0:
            raise RuntimeError("private Git history does not extend the leased base")
        try:
            # Before copying objects, every commit and every changed path needs the live lease.
            names = set()
            for sha in commits:
                changed = subprocess.run(host_git_argv("diff-tree", "--no-commit-id", "--name-only", "-r",
                                                       "-z", "--root", "-m", sha, git_dir=self.git_dir,
                                                       work_tree=self.guard.root),
                                         env=host_git_env(), capture_output=True, timeout=60, check=True)
                names.update(raw.decode("utf-8", "surrogateescape") for raw in changed.stdout.split(b"\0") if raw)
            if any(not self.guard.owned(name) for name in names):
                raise RuntimeError("one private commit touches a path outside the task lease")
            diff = subprocess.run(host_git_argv("diff", "--name-only", "-z", self.before, head,
                                               git_dir=self.git_dir, work_tree=self.guard.root),
                                  env=host_git_env(), capture_output=True, timeout=60, check=True)
            names = [raw.decode("utf-8", "surrogateescape") for raw in diff.stdout.split(b"\0") if raw]
            if not self.guard.allow_commits or any(not self.guard.owned(name) for name in names):
                raise RuntimeError("the private commit is outside the task lease")
            for xy, rel, original in self.guard._status():
                for path in (rel, original):
                    if path and path in names and not self.guard.owned(path):
                        raise RuntimeError("the command changed an unowned path: " + path)
            if self._git_head() != self.before:
                raise RuntimeError("the real leased ref changed during the command")
            bundle = self.root / "accepted.bundle"
            made = subprocess.run(host_git_argv("bundle", "create", str(bundle),
                                                self.guard.branch, "^" + self.before, "--",
                                                git_dir=self.git_dir, work_tree=self.guard.root),
                                  env=host_git_env(), stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=120)
            if made.returncode != 0:
                raise RuntimeError("the private commit ancestry could not be exported")
            # Importing a bundle brings all required commit, tree and blob objects, but no ref.
            fetched = subprocess.run(host_git_argv("-c", "protocol.file.allow=always", "fetch", "-q",
                                                  "--no-write-fetch-head", str(bundle), head,
                                                  git_dir=self.real_git, work_tree=self.guard.root),
                                     env=host_git_env(), stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=120)
            if fetched.returncode != 0 or not self._objects(head):
                raise RuntimeError("the real Git store rejected private commit objects")
            present: set[str] = set()
            for name in names:
                mode = subprocess.run(host_git_argv("ls-tree", head, "--", name,
                                                   git_dir=self.real_git, work_tree=self.guard.root),
                                      env=host_git_env(), stdin=subprocess.DEVNULL,
                                      capture_output=True, timeout=60, check=True)
                if not mode.stdout:
                    continue
                current = self.guard.root / name
                metadata = mode.stdout.split(None, 3)
                kind = metadata[1]
                stored = subprocess.run(host_git_argv("cat-file", kind.decode(), head + ":" + name,
                                                    git_dir=self.real_git, work_tree=self.guard.root),
                                       env=host_git_env(), stdin=subprocess.DEVNULL,
                                       capture_output=True, timeout=60, check=True).stdout
                if kind == b"blob":
                    text = stored.decode("utf-8", "surrogateescape")
                    if mode.stdout.startswith(b"120000 "):
                        target = Path(os.path.normpath(current.parent / text))
                        if Path(self.guard.root) not in target.parents and target != self.guard.root:
                            raise RuntimeError("an escaping symlink is outside the task worktree")
                        if not current.is_symlink() or os.readlink(current) != text:
                            raise RuntimeError("in-worktree symlink source does not match its commit")
                    else:
                        if not current.is_file() or current.is_symlink() or current.read_bytes() != stored:
                            raise RuntimeError("source does not match the validated commit: " + name)
                    present.add(name)
                else:
                    raise RuntimeError("only file and symlink entries may be published")
            for name in names:
                current = self.guard.root / name
                if name not in present and os.path.lexists(current):
                    raise RuntimeError("deleted file remains in the worktree: " + name)
                if os.path.islink(current.parent):
                    raise RuntimeError("a published path crosses a source symlink")
            # No index mutation occurs before all source paths and entries validate.
            index = subprocess.run(host_git_argv("read-tree", head,
                                                git_dir=self.real_git, work_tree=self.guard.root),
                                   env=host_git_env(), stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=120)
            if index.returncode != 0:
                raise RuntimeError("the validated index could not be installed")
            update = subprocess.run(host_git_argv("update-ref", self.guard.branch, head, self.before,
                                                 git_dir=self.real_git, work_tree=self.guard.root),
                                    env=host_git_env(), stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=60)
            if update.returncode != 0:
                raise RuntimeError("the leased branch did not accept the new commit")
            # The ref uses compare-and-swap after the exact source checks above.
            return head, names
        finally:
            self.guard.head = self._git_head()

    def _git_head(self) -> str:
        from .harness import host_git_argv, host_git_env
        proc = subprocess.run(host_git_argv("rev-parse", "--verify", "-q", self.guard.branch,
                                           git_dir=self.real_git, work_tree=self.guard.root),
                              env=host_git_env(), stdin=subprocess.DEVNULL, capture_output=True)
        return proc.stdout.decode("ascii", "replace").strip()
