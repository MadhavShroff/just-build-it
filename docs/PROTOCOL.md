# just-build-it wire protocol — version 1

This file is normative. `jbi/protocol.py` implements it. `GET /api/v1/schema` serves it as data.

## Principles

1. **One state document.** The server owns one JSON document per run. Every client renders that document.
2. **One change format.** Every change is an event that carries RFC 6902 JSON Patch operations on the document.
3. **One write path.** Every write is a command posted to one endpoint. A command handler emits events. Nothing else changes state.
4. **One channel for every client.** The browser, the CLI, and scripts use the same HTTP contract.
5. **Self-description.** `GET /api/v1/schema` lists every command, its payload fields, and every event type.

## Versioning

- Every response body carries `"protocol": 1`.
- A client sends `Accept-Protocol: 1`. The server returns `406` with error code `protocol_unsupported` if it cannot serve that version.
- A new optional field does not change the version. A removed or renamed field does.

## Authentication

- The CLI prints links of the form `http://127.0.0.1:<port>/#k=<key>`.
- The browser posts `{"key": "<key>"}` to `POST /api/v1/session`. The server sets the cookie `jbi_session` (HttpOnly, SameSite=Strict, Path=/).
- A key works once. The CLI menu (`n`), `just-build-it links`, and `POST /api/v1/links` print new links.
- Only the bearer token can create links. A browser session cannot, so a signed-in page cannot hand out access.
- A machine client sends `Authorization: Bearer <token>`. The token is in `<run-dir>/control.json`, file mode 0600.
- A state-changing request needs a valid session or bearer token, `Content-Type: application/json`, and, for cookie sessions, an `Origin` that equals the request `Host`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/session` | Exchange a one-time key for a session cookie. |
| `GET` | `/api/v1/state` | Read the full state document. |
| `GET` | `/api/v1/events?after=<seq>` | Server-Sent Events stream of events after `seq`. |
| `GET` | `/api/v1/events.json?after=<seq>&limit=<n>` | The same events as a JSON page, for polling clients. |
| `POST` | `/api/v1/commands` | Submit a command. |
| `POST` | `/api/v1/links` | Bearer token only. Returns `{protocol, links: [{label, url}]}`: one fresh one-time sign-in link per verified listener. |
| `GET` | `/api/v1/schema` | Command and event catalog. |
| `GET` | `/api/v1/health` | Readiness. No authentication. Returns `{protocol, ok, run_id, version}`. `version` is the server's release string. |

## State read

```json
GET /api/v1/state
200 {"protocol": 1, "seq": 42, "state": { ...document... },
     "info": {"pid": 4711, "run_dir": "/home/u/.just-build-it/runs/r-20261104-1502"}}
```

`seq` is the sequence number of the last event applied to the document.
`info` describes the serving process: `pid` is the CLI process id, and `run_dir` holds the run record. `info` is not part of the state document and has no events. A server without a CLI can send `{}`.

## Events

An event is one JSON object:

```json
{"seq": 43, "ts": "2026-11-04T15:02:11.204Z", "type": "task.status",
 "ops": [{"op": "replace", "path": "/tasks/core/status", "value": "running"}],
 "meta": {"task": "core"}}
```

- `seq` increases by one for each event. There are no gaps.
- `ts` is UTC, ISO 8601, millisecond precision.
- `type` names the change. Clients use `type` and `meta` for notifications. Clients use `ops` to update state.
- `ops` uses `add`, `remove`, and `replace` only. Paths use RFC 6901 escaping.

### Stream rules

- The SSE `id` field equals `seq`. The SSE `event` field is `patch`. The SSE `data` field is the event JSON.
- A client reconnects with `Last-Event-ID` or `?after=<seq>`.
- If the server no longer holds the events after `seq`, it sends one SSE event of type `reset`. The client then reads `/api/v1/state` again.
- The server sends a comment line `: ping` every 15 seconds.
- A client applies an event only if `event.seq == local_seq + 1`. On a gap the client reads `/api/v1/state` again.

## Commands

```json
POST /api/v1/commands
{"id": "c-7f3a", "type": "stack.accept", "payload": {"target": "~/bookshelf"}}

202 {"protocol": 1, "accepted": true, "id": "c-7f3a", "seq": 57}
```

- `id` is a client-chosen string of 1 to 64 characters. A repeated `id` returns the first result and does nothing again.
- `seq` in the reply is the last event that the command produced. A client can wait for that `seq` on the stream.
- The server validates the payload against the schema before the handler runs.
- The server runs one command at a time. Two posts with the same `id` at the same time run the handler once.

### Request bodies

- `Content-Length` must be one integer from 0 to 262144. A missing header means an empty body.
- A value that is not a non-negative integer gives `400 payload_invalid`. A value above the limit gives `413 too_large`. The server closes the connection after it refuses a `POST`.

### Errors

```json
409 {"protocol": 1, "error": {"code": "phase_invalid",
     "message": "stack.accept needs phase 'stack'; the run is in phase 'intake'.",
     "details": {"expected": "stack", "actual": "intake"}}}
```

| HTTP | Code | Meaning |
|---|---|---|
| 400 | `payload_invalid` | The payload does not match the command schema. |
| 400 | `command_unknown` | The command type is not in the catalog. |
| 401 | `unauthenticated` | No valid session or token. |
| 403 | `origin_rejected` | The `Origin` header does not match. |
| 404 | `not_found` | No endpoint has this method and path, or the payload names a task that is not in the plan. |
| 406 | `protocol_unsupported` | The protocol version is not served. |
| 409 | `phase_invalid` | The command is not valid in the current phase. |
| 409 | `conflict` | The command conflicts with the current state, for example a cycle. |
| 413 | `too_large` | The body is larger than 262144 bytes. |
| 415 | `media_unsupported` | The body is not `application/json`. |
| 500 | `internal` | A server defect. The message never contains a secret. |

### CLI client errors

`just-build-it status`, `links` and `send` print an error object `{"error": {code, message}}` on stderr and exit 1. They use the server codes above, plus `unreachable` when the dashboard does not answer because the run has stopped.

## Command catalog

`payload` fields are required unless marked optional.

| Type | Phase | Payload |
|---|---|---|
| `intake.import` | intake | `text`: string |
| `intake.say` | intake | `text`: string |
| `intake.detail` | intake | `level`: `user` \| `guided` \| `delegate` |
| `intake.finish` | intake | — |
| `stack.revise` | stack | `request`: string |
| `stack.accept` | stack | `target`: string, optional `name`: string |
| `stack.back` | stack, bootstrap | — From `stack`: back to `intake`. From `bootstrap`: only when `/bootstrap/status` is `failed`; back to `stack`. |
| `bootstrap.retry` | bootstrap | — Only when `/bootstrap/status` is `failed`. Runs bootstrap again at the same target. |
| `run.configure` | configure, execute | `rigor`: level, `profile`: profile |
| `run.start` | configure | — |
| `task.override` | configure, execute | `task`: id, optional `rigor`, optional `profile`, optional `model` |
| `task.retry` | execute | `task`: id |
| `task.pause` | execute | `task`: id |
| `task.resume` | execute | `task`: id |
| `decision.answer` | any | `decision`: id, `answer`: string |
| `account.set` | any | `provider`: id, `enabled`: bool, optional `api_key`: string |
| `account.probe` | any | `provider`: id |
| `architecture.refresh` | configure, execute, complete | — |
| `run.stop` | any | `mode`: `quit` \| `standdown` \| `finish` \| `kill` |

An `api_key` goes to the keychain. The server never echoes the key, never stores the key in the event log, and never logs the key.

## Event catalog

| Type | Meaning |
|---|---|
| `run.created` | The run document exists. |
| `run.phase` | The phase changed. |
| `run.config` | Rigor, profile, or limits changed. |
| `run.stopping` | A stop mode started. |
| `chat.message` | A chat message was added. |
| `plan.updated` | The rough PRD, PRD, or plan changed. |
| `stack.proposed` | A stack proposal is ready. |
| `bootstrap.status` | The bootstrap step changed. |
| `task.status` | A task status changed. |
| `task.stage` | A task stage started, passed, or failed. |
| `task.override` | A per-task override changed. |
| `session.status` | An agent session started, changed, or ended. |
| `lane.status` | A CI lane changed. |
| `decision.opened` | The run needs an answer from the user. |
| `decision.answered` | The user answered. |
| `architecture.updated` | The architecture graph changed. |
| `telemetry.sample` | A usage or timing sample was recorded. |
| `account.status` | A provider account or probe result changed. |
| `log.line` | An activity line for the timeline. |

## State document

```json
{
  "run": {"id": "r-20261104-1502", "name": "bookshelf", "phase": "execute",
          "mode": "new", "target": "/home/u/bookshelf", "created": "...",
          "stopping": null, "busy": {"label": "Writing the plan", "since": "..."},
          "tmux": "tmux -L jbi-r-20261104-1502 attach"},
  "config": {"rigor": "high", "profile": "balanced", "max_sessions": 3, "lanes": 4},
  "accounts": {"claude": {"enabled": true, "status": "ok", "checked": "...", "detail": "..."}},
  "intake": {"detail": "guided", "messages": [{"role": "user", "text": "...", "ts": "..."}]},
  "plan": {"revision": 3, "rough_prd": "...", "prd": {}, "tasks": {}, "order": [], "waves": []},
  "stack": {"layers": [{"layer": "backend", "choice": "...", "reason": "...", "cost": "free"}],
            "summary": "...", "accepted": false},
  "bootstrap": {"status": "running", "detail": "...", "commit": null},
  "sessions": {"s-core-1": {"task": "core", "role": "implement", "model": "...",
               "window": "core:implement", "status": "running", "started": "..."}},
  "lanes": {"lane-1": {"status": "testing", "task": "core", "base": "abc123", "head": "def456"}},
  "decisions": {"d-1": {"title": "...", "body": "...", "options": ["..."], "status": "open"}},
  "architecture": {"revision": 2, "nodes": {}, "edges": {}},
  "telemetry": {"tokens_in": 0, "tokens_out": 0, "cost_usd": null, "samples": 0},
  "timeline": [{"seq": 41, "ts": "...", "kind": "task", "text": "core passed acceptance"}]
}
```

`timeline` keeps the last 200 lines. The event log keeps the full history.

`run.busy` is `null` when no model work runs. While the planner works on a model call, `run.busy` is `{label, since}`: `label` names the step for the user, and `since` is the start time. A `run.config` event sets and clears it. A second model command during this time gets `409 conflict`.

A task `status` is one of `blocked`, `ready`, `running`, `verifying`, `queued`, `merging`, `merged`, `failed`, `paused`, `decision`, or `skipped`. `skipped` means the user chose to skip the task. A skipped task has no merge commit, and its dependents stay `blocked`.

The runtime also writes these task fields:

| Field | Meaning |
|---|---|
| `stages` | Each stage name maps to `{status, detail, model}`. |
| `attempts` | Failed attempts so far. Two failures move the implementer up one tier. |
| `attempt_history` | One entry for each attempt, kept across retries for escalation. |
| `resume_after` | The stage after which a paused task continues. |
| `blocked_reason` | Why a task cannot start, for example `dependency core was skipped`. |

`bootstrap.branch` names the repository branch that tasks merge into. A new repository uses `main`. An existing repository keeps its own current branch.

A timeline line or an event never holds provider output. A failed model call names the provider, the exit code, and a fixed sentence. The run directory keeps the details in `debug.log` (file mode 0600), with token-like strings removed.
