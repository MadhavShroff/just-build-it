"""Secret storage in the operating system keychain.

A value goes to `security` (macOS) or `secret-tool` (Linux) on standard input, never on a
command line, because a command line is visible to every process on the machine.
Without a keychain tool, or when the keychain tool fails, the value lives in process memory
for the run. `set()` returns "memory" in that case, so the dashboard can say so.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

SERVICE = "just-build-it"
TIMEOUT = 10
_memory: dict[str, str] = {}


def backend() -> str:
    """The storage in use: "keychain-macos", "keychain-linux" or "memory"."""
    if sys.platform == "darwin" and shutil.which("security"):
        return "keychain-macos"
    if sys.platform != "darwin" and shutil.which("secret-tool"):
        return "keychain-linux"
    return "memory"


def _run(argv: list[str], stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=TIMEOUT)


def _quote(value: str) -> str:
    """Quote a value for the `security -i` command parser."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set(name: str, value: str) -> str:  # noqa: A001 - the module API is set/get
    """Store a value. Returns the storage that holds it: a keychain backend or "memory".

    A keychain tool can exist and still fail, for example with no D-Bus session. The value
    then stays in process memory for this run, so the key is never lost and never a 500.
    """
    kind = backend()
    try:
        if kind == "keychain-macos":
            # `security -i` reads commands from stdin, so the value never appears in argv.
            command = f"add-generic-password -U -a {_quote(name)} -s {_quote(SERVICE)} -w {_quote(value)}\n"
            done = _run(["security", "-i"], command)
        elif kind == "keychain-linux":
            done = _run(["secret-tool", "store", f"--label={SERVICE} {name}",
                         "service", SERVICE, "account", name], value)
        else:
            done = None
        ok = done is not None and done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if ok:
        _memory.pop(name, None)
        return kind
    _memory[name] = value
    return "memory"


def get(name: str) -> str | None:
    if name in _memory:                         # memory holds the newest value after a keychain failure
        return _memory[name]
    kind = backend()
    try:
        if kind == "keychain-macos":
            done = _run(["security", "find-generic-password", "-a", name, "-s", SERVICE, "-w"])
        elif kind == "keychain-linux":
            done = _run(["secret-tool", "lookup", "service", SERVICE, "account", name])
        else:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.rstrip("\n") if done.returncode == 0 and done.stdout else None
