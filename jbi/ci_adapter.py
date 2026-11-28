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
from inspect import signature
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import worktrees as wt
from .protocol import ProtocolError

Runner = Callable[..., subprocess.CompletedProcess]
PR_URL_RE = re.compile(r"/pull/(\d+)\s*$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
# The symbolic job id the service's `ready` reply names (`job="pr-<N>"`). Its `job`
# verb resolves lane job ids (`YYYYMMDD-HHMMSS-lN-gX`) and may answer `no-such-job`
# for the symbolic form while the pull request only sits queued.
PR_JOB_RE = re.compile(r"^pr-\d+$")
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


def default_runner(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env)


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

    def __init__(self, runner: Runner = default_runner, executable: str = DEFAULT_EXECUTABLE,
                 env: dict[str, str] | None = None):
        self.runner = runner
        if executable == DEFAULT_EXECUTABLE:
            executable = os.environ.get("JBI_CI_CONDUCTOR") or executable
        self.executable = executable
        # Identity handed to every service call, so the CI service can route its
        # completion notices to this run instead of the caller's tmux pane. The
        # subprocess environment is the one channel the service already reads (the
        # caller-slot variable names the notice owner); extra keys are ignored until
        # the service grows a callback. Credentials never travel here.
        self.env = dict(env or {})
        # A legacy two-argument runner keeps working: the identity environment rides
        # only on runners that declare the third parameter.
        try:
            self._runner_takes_env = len(signature(self.runner).parameters) >= 3
        except (TypeError, ValueError):
            self._runner_takes_env = False

    def _exec(self, argv: list[str], cwd: Path) -> str:
        res = self.runner(argv, cwd)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            raise CIAdapterError(f"{' '.join(argv[:3])} failed (exit {res.returncode}): {detail}")
        return (res.stdout or "").strip()

    # ---- git and gh -------------------------------------------------------------------

    def push_branch(self, repo: Path, branch: str) -> None:
        # HOST_CONFIG disables the repository's hooks (a pre-push guard would reject
        # every `jbi/<task>` branch under a tmux session), monitors, and external diff.
        self._exec(["git", *wt.IDENTITY, *wt.HOST_CONFIG, "-C", str(repo), "push", "-u", "origin", branch], repo)

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
        argv = [self.executable, verb, *args]
        if self.env and self._runner_takes_env:
            res = self.runner(argv, repo, {**os.environ, **self.env})
        else:
            res = self.runner(argv, repo)
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
        existing_pr = task.get("pr_number")
        if isinstance(existing_pr, int) and existing_pr > 0:
            try:
                pr_data = self._github_pr(repo, existing_pr, ("state", "headRefOid"))
                if str(pr_data.get("state") or "").upper() == "MERGED" and pr_data.get("headRefOid") == head_sha:
                    reply = {
                        "type": "queued",
                        "job": f"pr-{existing_pr}",
                        "already_merged": True,
                        "text": f"#{existing_pr} at {head_sha[:9]} is already merged on GitHub.",
                        "bound": {"task": tid, "verb": verb, "pr_number": existing_pr,
                                  "head_sha": head_sha, "base": base, "base_commit": base_commit},
                    }
                    return HandoverResult(task=tid, branch=branch, pr_number=existing_pr,
                                          head_sha=head_sha, reply=reply, base_commit=base_commit)
            except CIAdapterError:
                pass
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

    def _github_pr(self, repo: Path, number: int, fields: tuple[str, ...]) -> dict[str, Any]:
        """Read one pull request from GitHub through `gh`. No credential ever prints."""
        out = self._exec(["gh", "pr", "view", str(number), "--json", ",".join(fields)], repo)
        try:
            data = json.loads(out)
        except ValueError as error:
            raise CIAdapterError(f"gh pr view returned invalid JSON: {error}") from error
        if not isinstance(data, dict):
            raise CIAdapterError("gh pr view returned no object")
        return data

    def _github_says_merged(self, repo: Path, number: int) -> bool:
        data = self._github_pr(repo, number, ("state",))
        return str(data.get("state") or "").upper() == "MERGED"

    def _fetch_base(self, repo: Path, result: HandoverResult) -> None:
        """Pull the base from origin so a service-side squash commit resolves here.

        The CI service merges on GitHub and fetches inside its own lanes; this
        repository has never seen the squash commit. Without the fetch the local
        `rev-parse` below fails on every merge."""
        base = (result.reply.get("bound") or {}).get("base") or wt.MAIN
        wt.fetch_branch(repo, str(base))

    def pending(self, repo: Path, result: HandoverResult) -> bool:
        """Read the direct job record. A running job has not produced a verdict.

        A queued symbolic `pr-<N>` job stays active until the merge is proven. The
        service answers `no-such-job` while it cannot resolve the symbolic id against
        a lane, and that is a lookup gap, never a lost handover: the pull request
        record on GitHub decides, so the wait ends the moment the merge lands."""
        job = result.reply.get("job")
        if not job:
            raise CIAdapterError("the handover has no direct job identifier")
        if result.reply.get("already_merged"):
            return False
        job = str(job)
        try:
            reply = self._service(repo, "job", [job])
        except CIAdapterError as error:
            if PR_JOB_RE.match(job) and "no-such-job" in str(error):
                try:
                    return not self._github_says_merged(repo, result.pr_number)
                except CIAdapterError:
                    # GitHub is unreadable too: the queue is all anyone knows, so the
                    # queued pull request keeps waiting instead of aborting the merge.
                    return True
            raise
        return job_is_active(str(reply.get("text") or ""))

    def _confirm_symbolic_merge(self, repo: Path, result: HandoverResult, job: str,
                                tracking: Any | None = None) -> dict[str, Any]:
        """Complete a `pr-<N>` handover whose job record the service cannot resolve.

        The CI service merges a pull request only after its own green verdict, so a
        MERGED state on GitHub at this exact head is the merge evidence. The squash
        commit must still resolve here after a fetch of the base."""
        number, head = result.pr_number, result.head_sha
        self._fetch_base(repo, result)
        data = self._github_pr(repo, number, ("state", "headRefOid", "mergeCommit"))
        state = str(data.get("state") or "").upper()
        if state != "MERGED":
            raise CIAdapterError(f"job {job} has no record and PR #{number} is not merged "
                                 f"(state {state or 'unknown'})")
        merged_head = str(data.get("headRefOid") or "")
        if merged_head and merged_head != head:
            raise CIAdapterError(f"PR #{number} merged at head {merged_head[:9]}, not {head[:9]}")
        merge_oid = str((data.get("mergeCommit") or {}).get("oid") or "")
        if not re.fullmatch(r"[0-9a-f]{7,40}", merge_oid):
            raise CIAdapterError(f"PR #{number} names no merge commit")
        squash = self._exec(["git", *wt.IDENTITY, *wt.HOST_CONFIG, "-C", str(repo), "rev-parse", "--verify",
                             f"{merge_oid}^{{commit}}"], repo)
        if not _full_sha(squash):
            raise CIAdapterError(f"merge commit {merge_oid} does not resolve to a full commit")
        reply = {"type": "data", "job": job, "source": "github",
                 "text": f"#{number} merged at head {head[:9]}; the job record {job} is "
                         "unresolvable, the merge evidence comes from GitHub"}
        evidence = {"job": job, "pr_number": number, "head_sha": head, "squash_sha": squash,
                    "green_run": job, "reply": reply}
        if tracking is not None:
            tracking.record_ci(result.task, pr_number=number, head_sha=head, base_commit=result.base_commit,
                               service_reply={**reply, "bound": result.reply.get("bound")},
                               green_run=job, squash_sha=squash, state="green")
            tracking.record_merge(result.task, number, head, squash, job)
        return evidence

    def confirm_merge(self, repo: Path, result: HandoverResult, *, job: str | None = None,
                      tracking: Any | None = None) -> dict[str, Any]:
        """Read the job record. Return merge evidence, or raise when any piece is missing.

        A `submit` reply never proves a merge. The job text must show a green verdict, the
        exact PR at the exact head, and a merge line for that PR. A symbolic `pr-<N>`
        job the service cannot resolve falls back to the GitHub pull request record."""
        job = job or result.reply.get("job")
        if not job:
            raise CIAdapterError("no job id to read merge evidence from")
        job = str(job)
        if result.reply.get("already_merged") and PR_JOB_RE.match(job):
            return self._confirm_symbolic_merge(repo, result, job, tracking)
        try:
            reply = self._service(repo, "job", [job])
        except CIAdapterError as error:
            if PR_JOB_RE.match(job) and "no-such-job" in str(error):
                return self._confirm_symbolic_merge(repo, result, job, tracking)
            raise
        text = str(reply.get("text") or "")
        number, head = result.pr_number, result.head_sha
        if reply["type"] != "data":
            raise CIAdapterError(f"job {job} returned type {reply['type']}, not data")
        if f"#{number}@{head[:9]}" not in text:
            hotfix_merged = re.search(rf"#{number}->([0-9a-f]{{7,40}})\b", text)
            hotfix_ok = False
            if re.search(r"verdict\s+GREEN\b", text) and hotfix_merged:
                self._fetch_base(repo, result)
                try:
                    resolved = self._exec(
                        ["git", *wt.IDENTITY, *wt.HOST_CONFIG, "-C", str(repo), "rev-parse", "--verify",
                         f"{hotfix_merged.group(1)}^{{commit}}"],
                        repo,
                    )
                    hotfix_ok = _full_sha(resolved)
                except CIAdapterError:
                    hotfix_ok = False
            if not hotfix_ok:
                raise CIAdapterError(f"job {job} does not name PR #{number} at head {head[:9]}")
        if not re.search(r"verdict\s+GREEN\b", text):
            detail_lines = [f"job {job} has no green verdict"]
            for line in text.splitlines():
                stripped = line.rstrip()
                if stripped:
                    detail_lines.append(stripped)
            ev_ref = reply.get("evidence_url") or reply.get("evidence")
            if not ev_ref:
                ev_match = _EVIDENCE_LINE_RE.search(text)
                if ev_match:
                    ev_ref = ev_match.group(1)
            if ev_ref and _count_failure_lines(text) < 10:
                seen = set(detail_lines)
                for extra in extract_evidence_failures(ev_ref, max_lines=80):
                    if extra not in seen:
                        seen.add(extra)
                        detail_lines.append(extra)
            raise CIAdapterError("\n".join(detail_lines))
        merged = re.search(rf"#{number}->([0-9a-f]{{7,40}})\b", text)
        if not merged:
            if PR_JOB_RE.match(job):
                try:
                    return self._confirm_symbolic_merge(repo, result, job, tracking)
                except CIAdapterError:
                    raise CIAdapterError(f"job {job} has no merge record for PR #{number}") from None
            raise CIAdapterError(f"job {job} has no merge record for PR #{number}")
        self._fetch_base(repo, result)
        squash = self._exec(["git", *wt.IDENTITY, *wt.HOST_CONFIG, "-C", str(repo), "rev-parse", "--verify",
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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LINE_NUM_PREFIX_RE = re.compile(r"^\s*\d+:\s*")
_SUITE_FAILURE_RE = re.compile(
    r"^\s*FAIL\s+\S|\bTS\d+:|e01-census:|pre-merge-grant-check:"
)
_CASE_FAILURE_RE = re.compile(
    r"^\s*●\s+(?!Console\b)(?!\s*Cannot log after tests are done)"
)
_FAILURE_LINE_MARKERS: tuple[str, ...] = (
    "FAIL ",
    "● ",
    "Error",
    "Expected",
    "Received",
    "e01-census:",
    "unreviewed",
    "drifted",
    "refused",
    " conflict",
)
_TS_ERROR_RE = re.compile(r"\bTS\d+:")
_EVIDENCE_LINE_RE = re.compile(r"^\s*evidence(?:_url)?\s*[:=]?\s*(\S+)\s*$", re.M)
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


def _clean_log_line(line: str) -> str:
    return _ANSI_RE.sub("", line).rstrip()


def _is_ignored_log_noise(line: str) -> bool:
    body = _LINE_NUM_PREFIX_RE.sub("", line).lstrip()
    return (
        body.startswith("[Nest] ")
        or body.startswith("● Console")
        or "Cannot log after tests are done" in body
        or "Attempted to log " in body
        or "tmp/p02-census-results" in line
    )


def _is_ci_failure_line(line: str) -> bool:
    clean = _clean_log_line(line)
    if _is_ignored_log_noise(clean):
        return False
    return any(tok in clean for tok in _FAILURE_LINE_MARKERS) or bool(_TS_ERROR_RE.search(clean))


def _count_failure_lines(text: str) -> int:
    return sum(1 for line in str(text or "").splitlines() if _is_ci_failure_line(line))


def _resolve_evidence_run_log(evidence_ref: Any) -> Path | None:
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        return None
    clean = evidence_ref.strip()
    if clean.startswith("file://"):
        clean = clean[len("file://"):]
    if "://" in clean:
        return None
    candidate = Path(clean)
    try:
        if candidate.is_dir():
            log_file = candidate / "run.log"
        elif candidate.is_file() and candidate.name == "run.log":
            log_file = candidate
        else:
            return None
        return log_file if log_file.is_file() else None
    except OSError:
        return None


def extract_evidence_failures(evidence_ref: Any, *, max_lines: int = 80) -> list[str]:
    """Read up to `max_lines` relevant failure lines from `<evidence_dir>/run.log`."""
    log_file = _resolve_evidence_run_log(evidence_ref)
    if log_file is None or max_lines <= 0:
        return []
    try:
        with log_file.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _MAX_EVIDENCE_BYTES))
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    raw_lines = [_clean_log_line(ln) for ln in raw.splitlines()]
    out: list[str] = []
    seen: set[str] = set()
    # Pass 1A: suite-level FAIL / TSC / census / grant anchors first so failing file paths
    # never get starved by earlier case bullets; Pass 1B: case-level `● ` bullets.
    for anchor_re in (_SUITE_FAILURE_RE, _CASE_FAILURE_RE):
        for idx, line in enumerate(raw_lines):
            if not line.strip() or _is_ignored_log_noise(line):
                continue
            if not anchor_re.search(line):
                continue
            added_context = 0
            for pos in range(idx, min(len(raw_lines), idx + 14)):
                cand = raw_lines[pos]
                if not cand.strip() or _is_ignored_log_noise(cand):
                    continue
                if pos > idx and cand.startswith("PASS "):
                    break
                if cand not in seen:
                    seen.add(cand)
                    out.append(cand)
                    if len(out) >= max_lines:
                        return out
                if pos > idx:
                    added_context += 1
                    if added_context >= 5:
                        break
    if len(out) >= 5:
        return out
    # Pass 2 fallback when no Jest/TSC anchors matched (e.g. shell gate error).
    for idx, line in enumerate(raw_lines):
        if not line.strip() or not _is_ci_failure_line(line):
            continue
        for pos in (idx, idx + 1):
            if pos >= len(raw_lines):
                continue
            cand = raw_lines[pos]
            if not cand.strip() or _is_ignored_log_noise(cand) or cand in seen:
                continue
            if pos > idx and not any(k in cand for k in ("Expected", "Received", "at ", "Error", "FAIL ", "● ", "e01-census:")):
                continue
            seen.add(cand)
            out.append(cand)
            if len(out) >= max_lines:
                return out
    return out


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
    evidence = notice.get("evidence_url") or notice.get("evidence")
    if evidence:
        lines.append(f"evidence_url: {evidence}")
    if notice.get("retry_allowed") is False:
        lines.append("retry_allowed: false")
    excerpt = str(notice.get("first_failure_excerpt") or notice.get("detail") or "").strip()
    if excerpt:
        for line in excerpt.splitlines():
            stripped = _clean_log_line(line)
            if stripped and not _is_ignored_log_noise(stripped):
                lines.append(stripped)
    if evidence:
        seen = set(lines)
        for extra in extract_evidence_failures(evidence, max_lines=80):
            if extra not in seen:
                seen.add(extra)
                lines.append(extra)
    return lines[:100]


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
