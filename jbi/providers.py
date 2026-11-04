"""Provider adapters: the catalog, account probes, key storage and the real Completer.

This is the only module that talks to a model provider. A provider is a CLI in print
mode (`claude`, `codex`, `agy`) or an OpenAI-compatible HTTP endpoint. Provider CLIs
keep their own login; this module only asks them for their status. No secret goes on a
command line or into a log line.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import models, secrets
from .llm import Completer, Completion, CompletionError, Prompt

PROBE_TIMEOUT = 20
CALL_TIMEOUT = 600
BASE_URL_ENV = "JBI_OPENAI_BASE_URL"
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._:/-]*-[a-z0-9._:/-]*$")
DEBUG_LOG = "debug.log"                 # under the run dir, mode 0600, redacted
# The API key variable that each provider CLI reads. openai-compatible uses an HTTP header.
KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY", "agy": "GEMINI_API_KEY"}
# The brief pattern, plus optional space after ":" or "=" so "api-key: x" is caught too.
_TOKEN = re.compile(r"(?i)(sk-|ghp_|bearer\s+|api[_-]?key\s*[:=]\s*)\S+")
_LONG_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_debug_lock = threading.Lock()


@dataclass(frozen=True)
class ProbeResult:
    status: str                     # ok | failed | missing
    detail: str
    models: list[str] = field(default_factory=list)


def catalog() -> dict:
    return models.catalog()


def blank_account(name: str) -> dict:
    return {"label": catalog()["providers"][name]["label"], "enabled": False, "status": "unknown",
            "detail": "", "checked": None, "models": []}


def store_key(name: str, key: str) -> str:
    """Store an API key and return a note that is safe to show and to log.

    A keychain that fails does not lose the key: `secrets.set` keeps it in memory and
    says so, and the note tells the dashboard.
    """
    where = "the operating system keychain" if secrets.set(name, key) != "memory" else "memory for this run only"
    return f"Key stored in {where}"


def child_key_env(name: str) -> dict[str, str]:
    """The provider's API key variable, when the user stored a key for it. Else empty."""
    variable = KEY_ENV.get(name)
    key = secrets.get(name) if variable else None
    return {variable: key} if variable and key else {}


# ---- log safety ------------------------------------------------------------------------


def redact(text: str) -> str:
    """Remove token-like strings: key prefixes, bearer values, `api_key=` values, long base64 runs."""
    return _LONG_RUN.sub("<redacted>", _TOKEN.sub(lambda m: m.group(1) + "<redacted>", text))


def write_debug(path: Path | None, title: str, detail: str) -> None:
    """Append redacted details to the run's local debug log (mode 0600). Never raises."""
    if path is None:
        return
    try:
        with _debug_lock:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(f"--- {title}\n{redact(detail).rstrip()}\n")
    except OSError:
        pass


class CallFailed(ValueError):
    """A provider CLI exited non-zero. The message holds only the exit code."""

    def __init__(self, code: int, stderr: str, stdout: str):
        super().__init__(f"exit {code}")
        self.code, self.stderr, self.stdout = code, stderr, stdout


def _failure(provider: str, error: Exception) -> str:
    """A log-safe line for one failed call: the provider, the exit code and a fixed sentence."""
    if isinstance(error, CallFailed):
        return f"{provider}: exit {error.code}. The CLI call failed."
    if isinstance(error, urllib.error.HTTPError):
        return f"{provider}: HTTP {error.code}. The endpoint refused the call."
    if isinstance(error, subprocess.TimeoutExpired):
        return f"{provider}: the call timed out."
    if isinstance(error, (OSError, subprocess.SubprocessError)):
        return f"{provider}: the call could not run ({type(error).__name__})."
    return f"{provider}: the reply was not usable ({type(error).__name__})."


def _details(provider: str, model: str, error: Exception) -> str:
    lines = [f"{provider} {model}: {type(error).__name__}: {error}"]
    if isinstance(error, CallFailed):
        lines += [f"stderr: {error.stderr[-2000:]}", f"stdout: {error.stdout[-2000:]}"]
    return "\n".join(lines)


# ---- probes ----------------------------------------------------------------------------


def _run(argv: list[str], stdin: str | None = None, timeout: int = PROBE_TIMEOUT,
         env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    # No input means a closed stdin, so a CLI never waits on the terminal.
    return subprocess.run(argv, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout,
                          env=None if env is None else {**os.environ, **env})


def _catalog_models(name: str) -> list[str]:
    return list(dict.fromkeys(catalog()["providers"][name]["models"].values()))


def _probe_claude(_: str) -> ProbeResult:
    done = _run(["claude", "auth", "status"])
    try:
        status = json.loads(done.stdout)
    except json.JSONDecodeError:
        return ProbeResult("failed", "claude auth status gave no JSON. Run `claude auth login`.")
    if not status.get("loggedIn"):
        return ProbeResult("failed", "Not signed in. Run `claude auth login`.")
    method = status.get("authMethod")
    method = method if isinstance(method, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", method) else None
    return ProbeResult("ok", f"Signed in{f' with {method}' if method else ''}", _catalog_models("claude"))


def _probe_codex(_: str) -> ProbeResult:
    done = _run(["codex", "login", "status"])
    if done.returncode == 0:                    # a fixed sentence: CLI output never reaches the event log
        return ProbeResult("ok", "Signed in", _catalog_models("codex"))
    return ProbeResult("failed", "Not signed in. Run `codex login`.")


def _parse_model_ids(text: str) -> list[str]:
    try:
        data = json.loads(text)
        items = data.get("models", data) if isinstance(data, dict) else data
        return [m["id"] if isinstance(m, dict) else str(m) for m in items]
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
        pass
    found = []
    for line in text.splitlines():
        words = line.strip().lstrip("-*• ").split()
        if words and MODEL_ID.match(words[0].lower()):
            found.append(words[0])
    return list(dict.fromkeys(found))


def _probe_agy(_: str) -> ProbeResult:
    done = _run(["agy", "models"])
    ids = _parse_model_ids(done.stdout) if done.returncode == 0 else []
    if not ids:
        return ProbeResult("failed", "agy lists no models. Sign in with the agy app.")
    return ProbeResult("ok", f"{len(ids)} models listed", ids)


def _base_url() -> str | None:
    return (os.environ.get(BASE_URL_ENV) or "").rstrip("/") or None


def _http_headers() -> dict[str, str]:
    key = secrets.get("openai-compatible")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _probe_endpoint(_: str) -> ProbeResult:
    base = _base_url()
    if not base:
        return ProbeResult("missing", f"Set {BASE_URL_ENV} to the endpoint address.")
    request = urllib.request.Request(f"{base}/models", headers=_http_headers())
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            ids = [m["id"] for m in json.load(response).get("data", [])]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
        return ProbeResult("failed", f"{base}/models gave no model list: {type(error).__name__}")
    return ProbeResult("ok", f"{len(ids)} models at {base}", ids)


def _probe_simulator(_: str) -> ProbeResult:
    return ProbeResult("ok", "Always available", _catalog_models("simulator"))


PROBES: dict[str, Callable[[str], ProbeResult]] = {
    "claude": _probe_claude, "codex": _probe_codex, "agy": _probe_agy,
    "openai-compatible": _probe_endpoint, "simulator": _probe_simulator,
}


def probe(name: str) -> ProbeResult:
    """Check one provider. Never raises for an unavailable provider and never prints a secret."""
    cli = catalog()["providers"][name]["cli"]
    if cli and not shutil.which(cli):
        return ProbeResult("missing", f"The {cli} command is not installed.")
    try:
        result = PROBES[name](name)
        return ProbeResult(result.status, redact(result.detail), result.models)
    except subprocess.TimeoutExpired:
        return ProbeResult("failed", f"The probe timed out after {PROBE_TIMEOUT} seconds.")
    except OSError as error:
        return ProbeResult("failed", f"The probe could not run: {type(error).__name__}")


# ---- completions -----------------------------------------------------------------------


def _full_prompt(prompt: Prompt) -> str:
    tail = "\n\nReply with one JSON value and no other text." if prompt.expect_json else ""
    return f"{prompt.system}\n\n{prompt.user}{tail}"


def _first_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _usage(data: Any) -> tuple[int | None, int | None]:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None, None
    tokens_in = [usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                                        "prompt_tokens") if isinstance(usage.get(k), int)]
    tokens_out = usage.get("output_tokens", usage.get("completion_tokens"))
    return (sum(tokens_in) if tokens_in else None), (tokens_out if isinstance(tokens_out, int) else None)


def _parse_claude(stdout: str) -> tuple[str, dict]:
    data = _first_json(stdout)
    if not isinstance(data, dict) or data.get("is_error") or not isinstance(data.get("result"), str):
        raise ValueError("claude gave no result")
    tokens_in, tokens_out = _usage(data)
    cost = data.get("total_cost_usd")
    return data["result"], {"tokens_in": tokens_in, "tokens_out": tokens_out,
                            "cost_usd": cost if isinstance(cost, (int, float)) else None}


def _parse_codex(stdout: str) -> tuple[str, dict]:
    text, usage = "", {}
    for line in stdout.splitlines():
        event = _first_json(line.strip())
        if not isinstance(event, dict):
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text", text)
        if event.get("type") == "turn.completed":
            tokens_in, tokens_out = _usage(event)
            usage = {"tokens_in": tokens_in, "tokens_out": tokens_out}
    if not text:
        raise ValueError("codex gave no message")
    return text, usage


def _parse_agy(stdout: str) -> tuple[str, dict]:
    data = _first_json(stdout)
    if isinstance(data, dict):
        text = next((data[k] for k in ("result", "response", "text", "output") if isinstance(data.get(k), str)), "")
        tokens_in, tokens_out = _usage(data)
        if text:
            return text, {"tokens_in": tokens_in, "tokens_out": tokens_out}
    if stdout.strip() and data is None:
        return stdout.strip(), {}
    raise ValueError("agy gave no result")


def _cli_call(provider: str, model: str, prompt: Prompt, timeout: int) -> tuple[str, dict]:
    text, env = _full_prompt(prompt), child_key_env(provider)
    if provider == "claude":
        argv, stdin, parse = ["claude", "-p", "--output-format", "json", "--model", model], text, _parse_claude
    elif provider == "codex":
        argv, stdin, parse = ["codex", "exec", "--json", "-m", model], text, _parse_codex
    else:
        argv, stdin, parse = ["agy", "-p", "--model", model, "--output-format", "json", text], None, _parse_agy
    done = _run(argv, stdin, timeout, env)
    if done.returncode != 0:
        raise CallFailed(done.returncode, done.stderr or "", done.stdout or "")
    return parse(done.stdout)


def _http_call(model: str, prompt: Prompt, timeout: int) -> tuple[str, dict]:
    base = _base_url()
    if not base:
        raise ValueError(f"{BASE_URL_ENV} is not set")
    body = json.dumps({"model": model, "messages": [{"role": "system", "content": prompt.system},
                                                     {"role": "user", "content": prompt.user}]}).encode()
    request = urllib.request.Request(f"{base}/chat/completions", data=body, method="POST",
                                     headers={"Content-Type": "application/json", **_http_headers()})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    tokens_in, tokens_out = _usage(data)
    return data["choices"][0]["message"]["content"], {"tokens_in": tokens_in, "tokens_out": tokens_out}


def completer(state_reader: Callable[[], dict], *, timeout: int = CALL_TIMEOUT,
              debug_log: Path | None = None) -> Completer:
    """A Completer over the enabled accounts.

    It asks `models.select` for the provider and model, and falls back to the next
    usable provider with the same tier when a call fails. A CompletionError names each
    provider with its exit code and a fixed sentence only; the provider's own output goes
    to `debug_log`, redacted.
    """
    def complete(prompt: Prompt) -> Completion:
        state = state_reader()
        first = models.select(state, prompt.role)
        failures = []
        for provider in models.usable(state):
            if provider == "simulator":
                continue
            model = first.model if provider == first.provider else catalog()["providers"][provider]["models"][first.tier]
            try:
                if provider == "openai-compatible":
                    text, usage = _http_call(model, prompt, timeout)
                else:
                    text, usage = _cli_call(provider, model, prompt, timeout)
            except (ValueError, OSError, subprocess.SubprocessError, urllib.error.URLError, KeyError, IndexError,
                    TypeError) as error:
                failures.append(_failure(provider, error))
                write_debug(debug_log, f"{prompt.purpose or 'completion'} failed", _details(provider, model, error))
                continue
            return Completion(text=text, model=model, provider=provider, **usage)
        raise CompletionError("No enabled provider answered. " + ("; ".join(failures) or "Enable one on the Accounts panel."))
    return complete
