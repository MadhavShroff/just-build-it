"""Direct CI handover adapter and structured notice parser.

`CIAdapter.handover` pushes `jbi/<task>` with `git`, finds or creates a draft pull request
with `gh`, and hands the verified head to the CI service with
`<executable> <verb> <pr> --head <sha>`. The service replies with one JSON object. The
adapter validates the reply, and records it in `Tracking` when the caller supplies one.
The adapter never merges, never changes a pull request after it creates one, and never
treats a `submit` reply as proof of a merge. `confirm_merge` reads the job record and
demands a green verdict, the exact pull request, the exact head, and a merge record.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import worktrees as wt
from .protocol import ProtocolError

Runner = Callable[[list[str], Path], subprocess.CompletedProcess]
PR_URL_RE = re.compile(r"/pull/(\d+)\s*$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
REPLY_TYPES = frozenset({"ok", "done", "started", "queued", "data", "unchanged", "need", "refused", "failed"})
REFUSING_TYPES = frozenset({"need", "refused", "failed"})
VERBS = ("submit", "ready", "verify")
DEFAULT_EXECUTABLE = "ci-conductor"


class CIAdapterError(RuntimeError):
    """A git push, a GitHub PR call, or a CI service call failed or returned bad evidence."""


@dataclass(frozen=True)
class HandoverResult:
    task: str
    branch: str
    pr_number: int
    head_sha: str
    reply: dict[str, Any]
    base_commit: str = ""


def default_runner(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, stdin=subprocess.DEVNULL)


def _full_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA40_RE.match(value))


ACTIVE_WORDS = frozenset({"running", "queued", "pending", "started", "in_progress"})
_STATE_RE = re.compile(r"\b(?:state|status)\s*[:=]\s*([A-Za-z_]+)", re.I)
_VERDICT_RE = re.compile(r"\bverdict\s+(?:GREEN|RED|VOID)\b")
MAX_JOB_TEXT = 20000


def job_is_active(text: str) -> bool:
    """Return True when plain job text shows a job that still runs. No provider is called.

    The ci-conductor `job` verb prints `started <t>  finished <t|running>` and adds a
    `verdict ...` line only after the job ends."""
    text = text[:MAX_JOB_TEXT]
    if _VERDICT_RE.search(text):
        return False
    if re.search(r"\bfinished\s+running\b", text, re.I):
        return True
    state = _STATE_RE.search(text)
    if state:
        return state.group(1).lower() in ACTIVE_WORDS
    if re.search(r"\bfinished\s+\S", text, re.I) or re.search(
            r"\b(?:failed|success|complete|completed)\b", text, re.I):
        return False
    return bool(re.search(r"\b(?:running|queued|pending|started|in_progress)\b", text, re.I))


class CIAdapter:
    """Pushes task branches, creates draft PRs with `gh`, and calls the CI service directly."""

    def __init__(self, runner: Runner = default_runner, executable: str = DEFAULT_EXECUTABLE):
        self.runner = runner
        if executable == DEFAULT_EXECUTABLE:
            executable = os.environ.get("JBI_CI_CONDUCTOR") or executable
        self.executable = executable

    def _exec(self, argv: list[str], cwd: Path) -> str:
        res = self.runner(argv, cwd)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            raise CIAdapterError(f"{' '.join(argv[:3])} failed (exit {res.returncode}): {detail}")
        return (res.stdout or "").strip()

    # ---- git and gh -------------------------------------------------------------------

    def push_branch(self, repo: Path, branch: str) -> None:
        self._exec(["git", *wt.IDENTITY, "-C", str(repo), "push", "-u", "origin", branch], repo)

    def find_or_create_draft_pr(self, repo: Path, *, branch: str, base: str, title: str, body: str) -> int:
        listed = self._exec(["gh", "pr", "list", "--head", branch, "--state", "open",
                             "--json", "number,baseRefName"], repo)
        if listed:
            try:
                items = json.loads(listed)
            except ValueError as error:
                raise CIAdapterError(f"gh pr list returned invalid JSON: {error}") from error
            if isinstance(items, list) and items and isinstance(items[0], dict) and "number" in items[0]:
                found_base = items[0].get("baseRefName")
                if found_base != base:
                    raise CIAdapterError(f"PR #{items[0]['number']} for {branch} targets {found_base!r}, not {base!r}")
                return int(items[0]["number"])
        created = self._exec(["gh", "pr", "create", "--draft", "--head", branch, "--base", base,
                              "--title", title, "--body", body], repo)
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

    # ---- the CI service ---------------------------------------------------------------

    def _service(self, repo: Path, verb: str, args: list[str]) -> dict[str, Any]:
        """Run one service verb. The reply must be one JSON object with a known `type`."""
        res = self.runner([self.executable, verb, *args], repo)
        out = (res.stdout or "").strip()
        try:
            reply = json.loads(out)
        except ValueError:
            detail = (res.stderr or out)[:200]
            raise CIAdapterError(f"ci {verb} returned no JSON (exit {res.returncode}): {detail}") from None
        if not isinstance(reply, dict) or reply.get("type") not in REPLY_TYPES:
            raise CIAdapterError(f"ci {verb} returned a reply without a known type")
        kind = reply["type"]
        if kind in REFUSING_TYPES:
            reason = reply.get("reason") or reply.get("error") or reply.get("missing") or reply.get("text") or kind
            raise CIAdapterError(f"ci {verb} {kind}: {reason}")
        if res.returncode != 0:
            raise CIAdapterError(f"ci {verb} exited {res.returncode} with a {kind} reply")
        return reply

    def _queue(self, repo: Path, verb: str, pr_number: int, head_sha: str) -> dict[str, Any]:
        if not _full_sha(head_sha):
            raise CIAdapterError(f"head {head_sha!r} is not a full lowercase 40-hex commit")
        if verb == "submit":
            reply = self._service(repo, verb, [str(pr_number)])
        else:
            reply = self._service(repo, verb, [str(pr_number), "--head", head_sha])
        text = str(reply.get("text") or "")
        if reply["type"] != "queued" or not reply.get("job"):
            raise CIAdapterError(f"ci {verb} did not queue PR #{pr_number}: reply type {reply['type']}")
        if verb == "submit":
            numbers = re.findall(r"#(\d+)\S*\s+queued(?:\s+at\s+([0-9a-f]{7,40}))?", text)
            if not any(int(number) == pr_number for number, _ in numbers):
                raise CIAdapterError(f"ci submit reply does not queue PR #{pr_number}")
        elif f"#{pr_number}" not in text or head_sha[:9] not in text:
            raise CIAdapterError(f"ci {verb} reply does not name PR #{pr_number} at head {head_sha[:9]}")
        return reply

    def mark_ready(self, repo: Path, pr_number: int, head_sha: str) -> dict[str, Any]:
        return self._queue(repo, "ready", pr_number, head_sha)

    def submit(self, repo: Path, pr_number: int, head_sha: str) -> dict[str, Any]:
        return self._queue(repo, "submit", pr_number, head_sha)

    def handover(self, repo: Path, task: dict[str, Any], *, base: str = wt.MAIN,
                 verb: str = "ready", tracking: Any | None = None) -> HandoverResult:
        if verb not in VERBS:
            raise CIAdapterError(f"unknown CI verb {verb!r}")
        tid = task["id"]
        branch = task.get("branch") or f"jbi/{tid}"
        head_sha = wt.head(repo, branch)
        if not _full_sha(head_sha):
            raise CIAdapterError(f"branch {branch} has no full 40-hex commit")
        base_commit = wt.head(repo, base)
        if not _full_sha(base_commit):
            raise CIAdapterError(f"base {base} has no full 40-hex commit")
        self.push_branch(repo, branch)
        title = task.get("title") or tid
        pr_number = self.find_or_create_draft_pr(repo, branch=branch, base=base, title=title,
                                                 body=task.get("goal") or title)
        reply = self._queue(repo, verb, pr_number, head_sha)
        reply = {**reply, "bound": {"task": tid, "verb": verb, "pr_number": pr_number, "head_sha": head_sha,
                                    "base": base, "base_commit": base_commit}}
        if tracking is not None:
            tracking.record_ci(tid, pr_number=pr_number, head_sha=head_sha, base_commit=base_commit,
                               service_reply=reply, state="submitted")
        return HandoverResult(task=tid, branch=branch, pr_number=pr_number, head_sha=head_sha,
                              reply=reply, base_commit=base_commit)

    def pending(self, repo: Path, result: HandoverResult) -> bool:
        """Read the direct job record. A running job has not produced a verdict."""
        job = result.reply.get("job")
        if not job:
            raise CIAdapterError("the handover has no direct job identifier")
        reply = self._service(repo, "job", [str(job)])
        return job_is_active(str(reply.get("text") or ""))

    def confirm_merge(self, repo: Path, result: HandoverResult, *, job: str | None = None,
                      tracking: Any | None = None) -> dict[str, Any]:
        """Read the job record. Return merge evidence, or raise when any piece is missing.

        A `submit` reply never proves a merge. The job text must show a green verdict, the
        exact PR at the exact head, and a merge line for that PR."""
        job = job or result.reply.get("job")
        if not job:
            raise CIAdapterError("no job id to read merge evidence from")
        reply = self._service(repo, "job", [str(job)])
        text = str(reply.get("text") or "")
        number, head = result.pr_number, result.head_sha
        if reply["type"] != "data":
            raise CIAdapterError(f"job {job} returned type {reply['type']}, not data")
        if f"#{number}@{head[:9]}" not in text:
            raise CIAdapterError(f"job {job} does not name PR #{number} at head {head[:9]}")
        if not re.search(r"verdict\s+GREEN\b", text):
            raise CIAdapterError(f"job {job} has no green verdict")
        merged = re.search(rf"#{number}->([0-9a-f]{{7,40}})\b", text)
        if not merged:
            raise CIAdapterError(f"job {job} has no merge record for PR #{number}")
        squash = self._exec(["git", *wt.IDENTITY, "-C", str(repo), "rev-parse", "--verify",
                             f"{merged.group(1)}^{{commit}}"], repo)
        if not _full_sha(squash):
            raise CIAdapterError(f"merge commit {merged.group(1)} does not resolve to a full commit")
        evidence = {"job": str(job), "pr_number": number, "head_sha": head, "squash_sha": squash,
                    "green_run": str(job), "reply": reply}
        if tracking is not None:
            tracking.record_ci(result.task, pr_number=number, head_sha=head, base_commit=result.base_commit,
                               service_reply={**reply, "job": str(job), "bound": result.reply.get("bound")},
                               green_run=str(job), squash_sha=squash, state="green")
            tracking.record_merge(result.task, number, head, squash, str(job))
        return evidence


def format_failure_findings(notice: dict[str, Any]) -> list[str]:
    """Turn a structured CI failure notice into bounded `findings` lines."""
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
