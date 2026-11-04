"""A small HTTP client for the wire protocol.

The CLI control menu, scripts and tests use this client. It talks to a running
dashboard exactly as the browser does, with the bearer token from `control.json`.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import PROTOCOL_VERSION


class ClientError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message, self.details = status, code, message, details or {}


@dataclass(frozen=True)
class Control:
    """The machine-readable control file that the CLI writes for each run (mode 0600)."""

    url: str
    token: str
    run_id: str
    pid: int

    @classmethod
    def load(cls, path: Path) -> "Control":
        data = json.loads(Path(path).read_text())
        return cls(data["url"], data["token"], data["run_id"], data["pid"])

    def save(self, path: Path) -> None:
        path = Path(path)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(json.dumps(self.__dict__, indent=2))


class Client:
    def __init__(self, url: str, token: str, timeout: float = 10.0):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    @classmethod
    def from_control(cls, control: Control) -> "Client":
        return cls(control.url, control.token)

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Accept-Protocol": str(PROTOCOL_VERSION),
                   "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                info = json.loads(error.read()).get("error", {})
            except (ValueError, AttributeError):
                info = {}
            raise ClientError(error.code, info.get("code", "http_error"),
                              info.get("message", error.reason), info.get("details")) from None

    def health(self) -> dict:
        return self._request("GET", "/api/v1/health")

    def state(self) -> dict:
        return self._request("GET", "/api/v1/state")

    def schema(self) -> dict:
        return self._request("GET", "/api/v1/schema")

    def events(self, after: int = 0, limit: int = 200) -> dict:
        return self._request("GET", f"/api/v1/events.json?after={after}&limit={limit}")

    def links(self) -> list[dict]:
        """Fresh one-time sign-in links, one per verified listener."""
        return self._request("POST", "/api/v1/links", {})["links"]

    def send(self, ctype: str, payload: dict | None = None, *, cid: str | None = None) -> dict:
        body = {"id": cid or f"cli-{secrets.token_hex(6)}", "type": ctype, "payload": payload or {}}
        return self._request("POST", "/api/v1/commands", body)

    def follow(self, after: int = 0, poll: float = 1.0) -> Iterator[dict]:
        """Yield every event after `after`, polling the JSON page endpoint."""
        import time
        cursor = after
        while True:
            page = self.events(cursor)
            for event in page["events"]:
                cursor = event["seq"]
                yield event
            if not page["more"]:
                time.sleep(poll)
