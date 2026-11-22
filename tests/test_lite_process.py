"""Process tests for the neutral lite harness. A real child process talks to a local fake endpoint."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from jbi.tracking import Tracking

ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-test-bearer-7731"
RECEIPT_TEXT = "BRIEF t1 | uncommitted | PASS\nDID: wrote src/new.txt"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


class FakeEndpoint:
    """A stdlib server for POST /v1/messages. It records each request and plays scripted answers."""

    def __init__(self, answers: list[dict]):
        self.answers, self.requests, self.headers = list(answers), [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "body": body})
                outer.headers.append({k: v for k, v in self.headers.items()})
                answer = outer.answers.pop(0)
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def final(text: str, usage: dict | None = None) -> dict:
    out = {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
    if usage is not None:
        out["usage"] = usage
    return out


def write_call(usage: dict | None = None) -> dict:
    out = {"content": [{"type": "tool_use", "id": "w1", "name": "write_file",
                        "input": {"path": "src/new.txt", "content": "hello\n"}}], "stop_reason": "tool_use"}
    if usage is not None:
        out["usage"] = usage
    return out


class ProcessBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lite-proc-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo = self.tmp / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.py").write_text("print('a')\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.db = self.tmp / "state" / "run.db"
        self.receipts = self.tmp / "receipts"
        self.receipts.mkdir()
        self.tracking = Tracking(self.db)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.tracking.close()
        except sqlite3.Error:
            pass

    def dispatch(self, continuation: bool = False, renew: bool = False) -> int:
        task = {"task_id": "t1", "owns": ["src"], "branch": "main", "worktree": self.repo}
        if renew:
            return self.tracking.renew_dispatch("t1", model="m", continuation=True)["dispatch_id"]
        self.tracking.create_task(title="t", owns=["src"], branch="main", worktree=self.repo,
                                  base_commit=self.base, task_id="t1")
        return self.tracking.dispatch("t1", session_id="s1", owns=task["owns"], branch="main",
                                      worktree=self.repo, model="m", continuation=continuation)["dispatch_id"]

    def spec(self, dispatch_id: int, **over) -> dict:
        spec = {"tracking": str(self.db), "workdir": str(self.repo), "model": "m", "task_id": "t1",
                "owns": ["src"], "session_id": "s1", "dispatch_id": dispatch_id, "continuation": False,
                "receipt": str(self.receipts / "r.receipt"), "checkpoint": str(self.tmp / "state" / "ck.json"),
                "timeout_sec": 60, "max_turns": 8, "effort": "high", "brief": "Write src/new.txt.",
                "repo_context": "REPO-CONTEXT", "frozen_contracts": "FROZEN-CONTRACTS",
                "usage_path": str(self.tmp / "state" / "usage.json")}
        spec.update(over)
        return spec

    def run_process(self, spec: dict, url: str | None = None, extra_env: dict | None = None):
        spec_path = self.tmp / "spec.json"
        spec_path.write_text(json.dumps(spec))
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp), "PYTHONPATH": str(ROOT)}
        if url:
            env.update({"CLAUDE_MUX_BASE_URL": url, "CLAUDE_MUX_API_KEY": SECRET})
        env.update(extra_env or {})
        return subprocess.run([sys.executable, "-m", "claude_mux_lite", str(spec_path)], env=env,
                              cwd=str(self.tmp), capture_output=True, text=True, timeout=120)

    def usage(self) -> dict:
        return json.loads(Path(self.tmp / "state" / "usage.json").read_text())

    def store_text(self) -> str:
        return json.dumps(self.tracking.fleet())


class FreshProcess(ProcessBase):
    def test_fresh_dispatch_end_to_end(self):
        answers = [write_call({"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 100,
                               "cache_creation_input_tokens": 50}),
                   final(RECEIPT_TEXT, {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 20,
                                        "cache_creation_input_tokens": 0})]
        dispatch_id = self.dispatch()
        with FakeEndpoint(answers) as ep:
            proc = self.run_process(self.spec(dispatch_id), ep.url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        first = ep.requests[0]
        self.assertEqual(first["path"], "/v1/messages")
        body = first["body"]
        self.assertEqual([t["name"] for t in body["tools"]],
                         ["bash", "read_file", "write_file", "edit_file", "list_dir", "grep_files"])
        self.assertIn("REPO-CONTEXT", body["system"][0]["text"])
        self.assertIn("FROZEN-CONTRACTS", body["system"][0]["text"])
        self.assertEqual(body["messages"][0]["content"][-1]["text"], "Brief:\nWrite src/new.txt.")
        self.assertEqual(ep.headers[0]["Authorization"], f"Bearer {SECRET}")
        self.assertEqual((self.repo / "src" / "new.txt").read_text(), "hello\n")
        self.assertEqual(len(ep.requests[1]["body"]["messages"]), 3)
        usage = self.usage()
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["cache_read"],
                          usage["cache_write"]), (15, 9, 120, 50))
        self.assertEqual((usage["dispatch_id"], usage["exit_code"], usage["turns"]), (dispatch_id, 0, 2))
        self.assertTrue(usage["final_text"].startswith("BRIEF t1 | uncommitted | PASS"))
        receipt = (self.receipts / "r.receipt").read_text()
        self.assertTrue(receipt.startswith("BRIEF t1 | uncommitted | PASS"))
        self.assertEqual(Path(self.tmp / "state" / "ck.json").exists(), True)

    def test_fresh_stores_no_history_no_cost_and_no_secret(self):
        dispatch_id = self.dispatch()
        with FakeEndpoint([final(RECEIPT_TEXT, {"input_tokens": 1})]) as ep:
            spec = self.spec(dispatch_id)
            proc = self.run_process(spec, ep.url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.tracking.session_history("s1"), [])
        self.assertEqual(self.tracking.fleet()["costs"], [])
        self.assertNotIn(SECRET, json.dumps(spec))
        for blob in (self.store_text(), (self.tmp / "state" / "usage.json").read_text(),
                     (self.tmp / "state" / "ck.json").read_text(), proc.stdout, proc.stderr):
            self.assertNotIn(SECRET, blob)
        for path in self.db.parent.iterdir():
            self.assertNotIn(SECRET.encode(), path.read_bytes())


class ContinuationProcess(ProcessBase):
    def test_continuation_persists_history_and_cost(self):
        first_id = self.dispatch(continuation=True)
        with FakeEndpoint([final(RECEIPT_TEXT, {"input_tokens": 3, "output_tokens": 4,
                                                "cache_read_input_tokens": 0,
                                                "cache_creation_input_tokens": 40})]) as ep:
            first = self.run_process(self.spec(first_id, continuation=True), ep.url)
        self.assertEqual(first.returncode, 0, first.stderr)
        history = self.tracking.session_history("s1")
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertNotIn("cache_control", json.dumps(history))
        second_id = self.dispatch(renew=True)
        self.assertNotEqual(first_id, second_id)
        with FakeEndpoint([final("BRIEF t1 | uncommitted | PASS", {"input_tokens": 2, "output_tokens": 1,
                                                                   "cache_read_input_tokens": 40,
                                                                   "cache_creation_input_tokens": 5})]) as ep:
            second = self.run_process(self.spec(second_id, continuation=True, brief="Second.",
                                                receipt=str(self.receipts / "r2.receipt")), ep.url)
        self.assertEqual(second.returncode, 0, second.stderr)
        messages = ep.requests[0]["body"]["messages"]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["content"][-1]["text"], "Brief:\nWrite src/new.txt.")
        self.assertEqual(messages[2]["content"][0]["text"], "Brief:\nSecond.")
        self.assertEqual(len(self.tracking.session_history("s1")), 4)
        fleet = self.tracking.fleet()
        costs = {c["dispatch_id"]: c for c in fleet["costs"]}
        self.assertEqual((costs[first_id]["input_tokens"], costs[first_id]["cache_write"]), (3, 40))
        self.assertEqual((costs[second_id]["cache_read"], costs[second_id]["cache_write"]), (40, 5))
        self.assertEqual((fleet["cache_read"], fleet["cache_write"]), (40, 45))
        self.assertEqual(len(fleet["dispatches"]), 2)
        self.assertEqual(len(fleet["leases"]), 1)
        self.assertEqual(self.usage()["dispatch_id"], second_id)
        self.assertNotIn(SECRET, self.store_text())


class FailureProcess(ProcessBase):
    def test_default_endpoint_unreachable_is_nonzero(self):
        dispatch_id = self.dispatch()
        proc = self.run_process(self.spec(dispatch_id), None, {"CLAUDE_MUX_BASE_URL": "http://127.0.0.1:1"})
        self.assertNotEqual(proc.returncode, 0)
        usage = self.usage()
        self.assertNotEqual(usage["exit_code"], 0)
        self.assertTrue(usage["final_text"].startswith("ERROR"))
        self.assertFalse((self.receipts / "r.receipt").exists())

    def test_default_process_without_endpoint_env_is_nonzero(self):
        dispatch_id = self.dispatch()
        proc = self.run_process(self.spec(dispatch_id), None)
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_usage_directory_is_nonzero(self):
        dispatch_id = self.dispatch()
        with FakeEndpoint([final(RECEIPT_TEXT)]) as ep:
            proc = self.run_process(self.spec(dispatch_id, usage_path=str(self.tmp / "nodir" / "u.json")),
                                    ep.url)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.tmp / "nodir").exists())

    def test_usage_absent_in_reply_stays_null(self):
        dispatch_id = self.dispatch()
        with FakeEndpoint([final(RECEIPT_TEXT)]) as ep:
            proc = self.run_process(self.spec(dispatch_id), ep.url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        usage = self.usage()
        for key in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
            self.assertIsNone(usage[key])

    def test_invalid_specs_are_nonzero(self):
        dispatch_id = self.dispatch()
        base = self.spec(dispatch_id)
        for missing in ("tracking", "usage_path", "workdir", "model", "task_id", "owns", "brief",
                        "checkpoint", "dispatch_id"):
            spec = {k: v for k, v in base.items() if k != missing}
            proc = self.run_process(spec, "http://127.0.0.1:1")
            self.assertNotEqual(proc.returncode, 0, missing)
        spec_path = self.tmp / "bad.json"
        spec_path.write_text("{not json")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT)}
        for args in ([], [str(spec_path)], [str(spec_path), "extra"], [str(self.tmp / "none.json")]):
            proc = subprocess.run([sys.executable, "-m", "claude_mux_lite", *args], env=env,
                                  capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0, args)

    def test_missing_tracking_db_fails_closed(self):
        dispatch_id = self.dispatch()
        gone = self.tmp / "other" / "run.db"
        with FakeEndpoint([final(RECEIPT_TEXT)]) as ep:
            proc = self.run_process(self.spec(dispatch_id, tracking=str(gone)), ep.url)
        self.assertNotEqual(proc.returncode, 0)
        usage = self.usage()
        self.assertNotEqual(usage["exit_code"], 0)
        self.assertFalse((self.receipts / "r.receipt").exists())
        self.assertNotIn("PASS", usage["final_text"])
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.tracking.fleet()["costs"], [])

    def test_unowned_write_is_refused_by_lease(self):
        dispatch_id = self.dispatch()
        call = {"content": [{"type": "tool_use", "id": "w1", "name": "write_file",
                             "input": {"path": "outside.txt", "content": "x"}}], "stop_reason": "tool_use"}
        with FakeEndpoint([call, final("BRIEF t1 | uncommitted | FAIL")]) as ep:
            self.run_process(self.spec(dispatch_id), ep.url)
        result = ep.requests[1]["body"]["messages"][-1]["content"][0]
        self.assertTrue(result.get("is_error"))
        self.assertFalse((self.repo / "outside.txt").exists())


class Neutrality(unittest.TestCase):
    def test_service_reads_no_system_script_or_registry(self):
        for name in ("__main__.py", "harness.py"):
            src = (ROOT / "claude_mux_lite" / name).read_text().lower()
            for word in ("registry", "tangram", "/usr/local", "~/.", ".secrets", "systemctl"):
                self.assertNotIn(word, src, f"{name}: {word}")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT)}
        proc = subprocess.run([sys.executable, "-c",
                               "import sys, claude_mux_lite.harness as h; "
                               "print([m for m in sys.modules if m.startswith('rswarm')])"],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "[]", proc.stderr)


if __name__ == "__main__":
    unittest.main()
