"""External CI handover adapter (`illm ci ready`) and structured notice parser.

When `--ci-service illm` is enabled, `Supervisor._merge` pushes `jbi/<task>` to `origin`,
finds or creates a draft pull request with `gh`, and hands the verified commit to the CI
service with `illm --json ci ready <pr> --head <sha>`. When a structured `illm.ci.failure.v1`
notice arrives via `ci.notice` (or the mailbox), the adapter matches `(pr_number, head_sha)`
to the waiting task and formats the failure fields into `findings` for the next coding
agent brief.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import worktrees as wt
from .protocol import ProtocolError

Runner = Callable[[list[str], Path], subprocess.CompletedProcess]
PR_URL_RE = re.compile(r"/pull/(\d+)\s*$")


class CIAdapterError(RuntimeError):
    """An external git push, GitHub PR, or `illm ci ready` call failed."""


@dataclass(frozen=True)
class HandoverResult:
    task: str
    branch: str
    pr_number: int
    head_sha: str
    reply: dict[str, Any]


def default_runner(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, stdin=subprocess.DEVNULL)


class CIAdapter:
    """Pushes task branches, manages draft PRs with `gh`, and calls `illm --json ci ready`."""

    def __init__(self, runner: Runner = default_runner):
        self.runner = runner

    def _exec(self, argv: list[str], cwd: Path) -> str:
        res = self.runner(argv, cwd)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            raise CIAdapterError(f"{' '.join(argv[:3])} failed (exit {res.returncode}): {detail}")
        return (res.stdout or "").strip()

    def push_branch(self, repo: Path, branch: str) -> None:
        self._exec(["git", *wt.IDENTITY, "-C", str(repo), "push", "-u", "origin", branch], repo)

    def find_or_create_draft_pr(self, repo: Path, *, branch: str, base: str, title: str, body: str) -> int:
        listed = self._exec(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number"],
            repo,
        )
        if listed:
            try:
                items = json.loads(listed)
            except ValueError as error:
                raise CIAdapterError(f"gh pr list returned invalid JSON: {error}") from error
            if isinstance(items, list) and items and isinstance(items[0], dict) and "number" in items[0]:
                return int(items[0]["number"])
        created = self._exec(
            ["gh", "pr", "create", "--draft", "--head", branch, "--base", base,
             "--title", title, "--body", body],
            repo,
        )
        if created.isdigit():
            return int(created)
        match = PR_URL_RE.search(created)
        if match:
            return int(match.group(1))
        try:
            data = json.loads(created)
            if isinstance(data, dict) and "number" in data:
                return int(data["number"])
        except ValueError:
            pass
        viewed = self._exec(["gh", "pr", "view", branch, "--json", "number"], repo)
        try:
            return int(json.loads(viewed)["number"])
        except (ValueError, KeyError, TypeError) as error:
            raise CIAdapterError(f"cannot determine PR number for {branch}: {created}") from error

    def mark_ready(self, repo: Path, pr_number: int, head_sha: str) -> dict[str, Any]:
        out = self._exec(["illm", "--json", "ci", "ready", str(pr_number), "--head", head_sha], repo)
        try:
            reply = json.loads(out) if out else {}
        except ValueError as error:
            raise CIAdapterError(f"illm ci ready returned invalid JSON: {out[:200]}") from error
        if not isinstance(reply, dict):
            raise CIAdapterError("illm ci ready returned a non-object JSON payload")
        status = reply.get("status")
        if status in ("refused", "failed", "need"):
            reason = reply.get("text") or reply.get("reason") or reply.get("error") or status
            raise CIAdapterError(f"illm ci ready {status}: {reason}")
        return reply

    def handover(self, repo: Path, task: dict[str, Any], *, base: str = wt.MAIN) -> HandoverResult:
        tid = task["id"]
        branch = task.get("branch") or f"jbi/{tid}"
        head_sha = wt.head(repo, branch) or task.get("commit")
        if not head_sha:
            raise CIAdapterError(f"branch {branch} has no commit")
        self.push_branch(repo, branch)
        title = task.get("title") or tid
        body = task.get("goal") or title
        pr_number = self.find_or_create_draft_pr(repo, branch=branch, base=base, title=title, body=body)
        reply = self.mark_ready(repo, pr_number, head_sha)
        return HandoverResult(task=tid, branch=branch, pr_number=pr_number, head_sha=head_sha, reply=reply)


def format_failure_findings(notice: dict[str, Any]) -> list[str]:
    """Turn a structured `illm.ci.failure.v1` notice into bounded `findings` lines."""
    verdict = str(notice.get("verdict") or "red")
    job_id = str(notice.get("job_id") or notice.get("job") or "unknown")
    pr_number = notice.get("pr_number")
    head_sha = notice.get("head_sha")
    header = f"CI {verdict} (job {job_id}"
    if pr_number is not None:
        header += f", PR #{pr_number}"
    if head_sha:
        header += f", head {str(head_sha)[:12]}"
    header += ")"
    lines = [header]
    gates = notice.get("failed_gates") or []
    if gates:
        lines.append("failed_gates: " + ", ".join(str(g) for g in gates))
    evidence = notice.get("evidence_url")
    if evidence:
        lines.append(f"evidence_url: {evidence}")
    if notice.get("retry_allowed") is False:
        lines.append("retry_allowed: false")
    excerpt = str(notice.get("first_failure_excerpt") or "").strip()
    if excerpt:
        for line in excerpt.splitlines():
            stripped = line.rstrip()
            if stripped:
                lines.append(stripped)
    return lines[:20]


def match_task(plan: dict[str, Any], notice: dict[str, Any]) -> str:
    """Resolve the task id in `plan` for a CI notice and verify `head_sha` matches `commit`."""
    tasks = plan.get("tasks") or {}
    tid = notice.get("task")
    pr_number = notice.get("pr_number")
    head_sha = notice.get("head_sha")
    if tid:
        task = tasks.get(tid)
        if task is None:
            raise ProtocolError("not_found", f"No task {tid!r} in the plan.", task=tid)
        if pr_number is not None and task.get("pr_number") is not None and int(task["pr_number"]) != int(pr_number):
            raise ProtocolError("conflict",
                                f"Notice pr_number {pr_number} does not match task PR {task['pr_number']}.",
                                task=tid)
        if head_sha and task.get("commit") and task["commit"] != head_sha:
            raise ProtocolError("conflict",
                                f"Notice head_sha {head_sha!r} does not match task commit {task['commit']!r}.",
                                task=tid)
        return str(tid)
    for candidate_id in plan.get("order") or list(tasks):
        task = tasks.get(candidate_id) or {}
        if pr_number is not None and task.get("pr_number") is not None and int(task["pr_number"]) == int(pr_number):
            if head_sha and task.get("commit") and task["commit"] != head_sha:
                raise ProtocolError("conflict",
                                    f"Notice head_sha {head_sha!r} does not match task commit {task['commit']!r}.",
                                    task=candidate_id)
            return str(candidate_id)
    if head_sha:
        for candidate_id in plan.get("order") or list(tasks):
            task = tasks.get(candidate_id) or {}
            if task.get("commit") == head_sha:
                return str(candidate_id)
    raise ProtocolError("not_found", "No task in the plan matches the CI notice.")
