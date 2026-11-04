"""The HTTP server: the protocol endpoints, Server-Sent Events, sessions and static files.

`docs/PROTOCOL.md` defines every endpoint. This module only translates HTTP to calls on
`Store` (reads) and `Engine.submit` (writes). It holds no business rules.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import PROTOCOL_VERSION, __version__, models, protocol, rigor
from .engine import Engine
from .protocol import ProtocolError
from .store import Store, iter_sse

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 256 * 1024
SESSION_COOKIE = "jbi_session"


@dataclass
class Auth:
    """One-time link keys, browser sessions and the machine bearer token."""

    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    _keys: set[str] = field(default_factory=set)
    _sessions: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def new_key(self) -> str:
        key = secrets.token_urlsafe(24)
        with self._lock:
            self._keys.add(key)
        return key

    def redeem(self, key: str) -> str | None:
        with self._lock:
            if key not in self._keys:
                return None
            self._keys.discard(key)
            session = secrets.token_urlsafe(32)
            self._sessions.add(session)
            return session

    def session_ok(self, session: str | None) -> bool:
        with self._lock:
            return bool(session) and any(hmac.compare_digest(session, s) for s in self._sessions)

    def bearer_ok(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value, self.token)


class App:
    """Everything a request handler needs. One instance per run."""

    def __init__(self, store: Store, engine: Engine, auth: Auth, *, extra_info: Callable[[], dict] | None = None,
                 link_bases: Callable[[], list[dict]] | None = None):
        self.store, self.engine, self.auth = store, engine, auth
        self.extra_info = extra_info or (lambda: {})
        # The verified base URLs ({label, url}) that sign-in links may point at.
        self.link_bases = link_bases or (lambda: [])
        self.shutdown = threading.Event()

    def schema(self) -> dict:
        return {**protocol.catalog(), "rigor_detail": rigor.describe(), "models": models.catalog(),
                "version": __version__}


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"just-build-it/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # silence default stderr logging
            return

        # ---- dispatch ------------------------------------------------------------------

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            url = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            route = (method, url.path)
            try:
                if url.path.startswith("/api/"):
                    self._check_protocol()
                    handler = API.get(route)
                    if handler is None:
                        raise ProtocolError("not_found", f"No endpoint {method} {url.path}.")
                    if route not in PUBLIC:
                        self._authenticate(write=(method == "POST" and route != ("POST", "/api/v1/session")))
                    handler(self, query)
                elif method == "GET":
                    self._static(url.path)
                else:
                    raise ProtocolError("not_found", f"No endpoint {method} {url.path}.")
            except ProtocolError as error:
                # A refused POST may leave its body unread. Close the connection, so those
                # bytes never parse as a second request.
                self._json(error.status, error.body(), {"Connection": "close"} if method == "POST" else None)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # never leak a traceback or a secret to the client
                import traceback
                traceback.print_exc()
                self._json(500, ProtocolError("internal", "The server hit a defect. See the CLI log.").body())

        # ---- checks --------------------------------------------------------------------

        def _check_protocol(self) -> None:
            wanted = self.headers.get("Accept-Protocol")
            if wanted and wanted.strip() != str(PROTOCOL_VERSION):
                raise ProtocolError("protocol_unsupported", f"This server speaks protocol {PROTOCOL_VERSION}.",
                                    supported=[PROTOCOL_VERSION])

        def _cookie(self) -> str | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == SESSION_COOKIE:
                    return value
            return None

        def _authenticate(self, *, write: bool) -> None:
            header = self.headers.get("Authorization") or ""
            if header.startswith("Bearer ") and app.auth.bearer_ok(header[7:].strip()):
                return
            if not app.auth.session_ok(self._cookie()):
                raise ProtocolError("unauthenticated", "Open the link that the CLI printed to sign in.")
            if write:
                origin, host = self.headers.get("Origin"), self.headers.get("Host")
                if not origin or urlsplit(origin).netloc != host:
                    raise ProtocolError("origin_rejected", "The request origin does not match this dashboard.")

        def _body(self) -> Any:
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                raise ProtocolError("media_unsupported", "Send the body as application/json.")
            length = _content_length(self.headers.get_all("Content-Length") or [])
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ProtocolError("payload_invalid",
                                    f"The body is not valid JSON: {getattr(error, 'msg', 'bad encoding')}.")

        # ---- responses -----------------------------------------------------------------

        def _headers(self, status: int, ctype: str, length: int | None, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                             "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()

        def _json(self, status: int, body: dict, extra: dict | None = None) -> None:
            data = json.dumps(body, separators=(",", ":")).encode()
            self._headers(status, "application/json; charset=utf-8", len(data), extra)
            self.wfile.write(data)

        def _static(self, path: str) -> None:
            relative = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (WEB_ROOT / relative).resolve()
            inside = WEB_ROOT in target.parents
            if not inside or not target.is_file():
                target = WEB_ROOT / "index.html"   # unknown paths and traversal fall back to the shell
            if not target.is_file():
                raise ProtocolError("not_found", "The dashboard files are not installed.")
            data = target.read_bytes()
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            if target.suffix == ".js":
                ctype = "text/javascript; charset=utf-8"
            self._headers(200, ctype, len(data))
            self.wfile.write(data)

        # ---- endpoints -----------------------------------------------------------------

        def health(self, query: dict) -> None:
            self._json(200, {"protocol": PROTOCOL_VERSION, "ok": True,
                             "run_id": app.store.read("/run/id"), "version": __version__})

        def session(self, query: dict) -> None:
            body = self._body()
            session = app.auth.redeem(str(body.get("key", ""))) if isinstance(body, dict) else None
            if session is None:
                raise ProtocolError("unauthenticated", "This link was already used or is not valid. "
                                                       "Ask the CLI for a new link.")
            cookie = f"{SESSION_COOKIE}={session}; HttpOnly; SameSite=Strict; Path=/"
            self._json(200, {"protocol": PROTOCOL_VERSION, "ok": True}, {"Set-Cookie": cookie})

        def state(self, query: dict) -> None:
            seq, doc = app.store.snapshot()
            self._json(200, {"protocol": PROTOCOL_VERSION, "seq": seq, "state": doc, "info": app.extra_info()})

        def schema(self, query: dict) -> None:
            self._json(200, app.schema())

        def events_json(self, query: dict) -> None:
            after, limit = _int(query, "after", 0), min(_int(query, "limit", 200), 1000)
            events = app.store.events_after(after, limit)
            self._json(200, {"protocol": PROTOCOL_VERSION, "events": events,
                             "seq": app.store.seq, "more": bool(events) and events[-1]["seq"] < app.store.seq})

        def events(self, query: dict) -> None:
            after = _int(query, "after", 0)
            last = self.headers.get("Last-Event-ID")
            if last and last.isdigit():
                after = int(last)
            self._headers(200, "text/event-stream; charset=utf-8", None,
                          {"Connection": "keep-alive", "X-Accel-Buffering": "no"})
            self.close_connection = True
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            for frame in iter_sse(app.store, after, stop=app.shutdown):
                self.wfile.write(frame)
                self.wfile.flush()

        def commands(self, query: dict) -> None:
            result = app.engine.submit(self._body())
            self._json(202, result)

        def links(self, query: dict) -> None:
            header = self.headers.get("Authorization") or ""
            if not (header.startswith("Bearer ") and app.auth.bearer_ok(header[7:].strip())):
                raise ProtocolError("unauthenticated", "New sign-in links need the bearer token.")
            links = [{"label": base["label"], "url": f"{base['url']}/#k={app.auth.new_key()}"}
                     for base in app.link_bases()]
            self._json(200, {"protocol": PROTOCOL_VERSION, "links": links})

    API: dict[tuple[str, str], Callable] = {
        ("GET", "/api/v1/health"): Handler.health,
        ("POST", "/api/v1/session"): Handler.session,
        ("GET", "/api/v1/state"): Handler.state,
        ("GET", "/api/v1/schema"): Handler.schema,
        ("GET", "/api/v1/events"): Handler.events,
        ("GET", "/api/v1/events.json"): Handler.events_json,
        ("POST", "/api/v1/commands"): Handler.commands,
        ("POST", "/api/v1/links"): Handler.links,
    }
    PUBLIC = {("GET", "/api/v1/health"), ("POST", "/api/v1/session")}
    return Handler


def _content_length(values: list[str]) -> int:
    """The body size: a missing header means 0; anything else must be one integer 0..MAX_BODY.

    The value goes to rfile.read, so a negative or unbounded size never gets through.
    """
    if not values:
        return 0
    text = values[0].strip()
    if len(values) > 1 or not (text.isascii() and text.isdigit()):
        raise ProtocolError("payload_invalid", "Content-Length must be one non-negative integer.")
    if len(text) > 12 or int(text) > MAX_BODY:
        raise ProtocolError("too_large", f"The body must be {MAX_BODY} bytes or fewer.")
    return int(text)


def _int(query: dict, name: str, default: int) -> int:
    value = query.get(name)
    if value is None:
        return default
    if not value.isdigit():
        raise ProtocolError("payload_invalid", f"Query parameter {name!r} must be a non-negative integer.")
    return int(value)


class Server:
    """One or more listeners (loopback, Tailscale) that share one App."""

    def __init__(self, app: App, hosts: list[str], port: int = 0):
        self.app = app
        handler = make_handler(app)
        self.listeners: list[ThreadingHTTPServer] = []
        for index, host in enumerate(hosts):
            bind_port = port if index == 0 else self.port
            server = ThreadingHTTPServer((host, bind_port), handler)
            server.daemon_threads = True
            self.listeners.append(server)
        self._threads = [threading.Thread(target=s.serve_forever, name=f"jbi-http-{s.server_address[0]}",
                                          daemon=True) for s in self.listeners]

    @property
    def port(self) -> int:
        return self.listeners[0].server_address[1]

    def addresses(self) -> list[tuple[str, int]]:
        return [(s.server_address[0], s.server_address[1]) for s in self.listeners]

    def start(self) -> "Server":
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        self.app.shutdown.set()
        for server in self.listeners:
            server.shutdown()
            server.server_close()


__all__ = ["App", "Auth", "Server"]
