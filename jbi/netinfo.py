"""Listener addresses and the links that the CLI prints.

The server listens on loopback. If Tailscale is running, it also listens on the
Tailscale IPv4 address, never on 0.0.0.0. The CLI prints a link only after a live
probe of that exact address returns the run id.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass

LOOPBACK = "127.0.0.1"


@dataclass(frozen=True)
class Tailnet:
    ip: str
    dns_name: str | None


@dataclass(frozen=True)
class Link:
    label: str          # "Local" or "Tailscale"
    host: str           # the address the server listens on
    url: str            # the base URL, without the one-time key
    verified: bool
    note: str = ""


def detect_tailscale(timeout: float = 3.0) -> Tailnet | None:
    """Return the Tailscale address if the daemon reports state Running, else None."""
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True,
                             timeout=timeout, check=False)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if data.get("BackendState") != "Running":
        return None
    me = data.get("Self") or {}
    ipv4 = next((ip for ip in me.get("TailscaleIPs", []) if ":" not in ip), None)
    if not ipv4:
        return None
    dns = (me.get("DNSName") or "").rstrip(".") or None
    return Tailnet(ipv4, dns)


def hosts(tailnet: Tailnet | None) -> list[str]:
    return [LOOPBACK] + ([tailnet.ip] if tailnet else [])


def probe(url: str, run_id: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/v1/health", timeout=timeout) as response:
            body = json.loads(response.read())
    except (OSError, ValueError):
        return False
    return body.get("ok") is True and body.get("run_id") == run_id


def links(port: int, run_id: str, tailnet: Tailnet | None) -> list[Link]:
    found = []
    local = f"http://{LOOPBACK}:{port}"
    found.append(Link("Local", LOOPBACK, local, probe(local, run_id)))
    if tailnet:
        url = f"http://{tailnet.ip}:{port}"
        ok = probe(url, run_id)
        found.append(Link("Tailscale", tailnet.ip, url, ok,
                          "" if ok else "the listener did not answer on the Tailscale address"))
    return found
