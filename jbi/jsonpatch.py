"""The RFC 6902 subset that the wire protocol uses: add, remove, replace.

The server builds operations with the helpers below and applies them with `apply`.
The browser applies the same operations with `web/js/patch.js`.
Paths use RFC 6901 escaping: `~` becomes `~0` and `/` becomes `~1`.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable

Op = dict[str, Any]


class PatchError(ValueError):
    """An operation does not fit the document."""


def escape(token: str) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def pointer(*tokens: Any) -> str:
    """Build a JSON pointer from raw tokens: pointer("tasks", "a/b") == "/tasks/a~1b"."""
    return "".join("/" + escape(str(t)) for t in tokens)


def split(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise PatchError(f"pointer must start with '/': {path!r}")
    return [unescape(t) for t in path[1:].split("/")]


def add(path: str, value: Any) -> Op:
    return {"op": "add", "path": path, "value": value}


def replace(path: str, value: Any) -> Op:
    return {"op": "replace", "path": path, "value": value}


def remove(path: str) -> Op:
    return {"op": "remove", "path": path}


def _parent(doc: Any, tokens: list[str]) -> tuple[Any, str]:
    node = doc
    for token in tokens[:-1]:
        if isinstance(node, dict):
            if token not in node:
                raise PatchError(f"missing key {token!r}")
            node = node[token]
        elif isinstance(node, list):
            node = node[_index(node, token, allow_end=False)]
        else:
            raise PatchError(f"cannot descend into {type(node).__name__}")
    return node, tokens[-1]


def _index(seq: list, token: str, *, allow_end: bool) -> int:
    if token == "-" and allow_end:
        return len(seq)
    if not token.isdigit():
        raise PatchError(f"bad array index {token!r}")
    index = int(token)
    limit = len(seq) + (1 if allow_end else 0)
    if index >= limit:
        raise PatchError(f"array index {index} out of range")
    return index


def apply_op(doc: Any, op: Op) -> Any:
    """Apply one operation in place and return the document (a new root for path "")."""
    kind, path = op.get("op"), op.get("path")
    if kind not in ("add", "remove", "replace") or not isinstance(path, str):
        raise PatchError(f"unsupported operation {op!r}")
    tokens = split(path)
    if not tokens:
        if kind == "remove":
            raise PatchError("cannot remove the root")
        return copy.deepcopy(op["value"])
    parent, last = _parent(doc, tokens)
    if isinstance(parent, dict):
        if kind != "add" and last not in parent:
            raise PatchError(f"{kind} of missing key {last!r}")
        if kind == "remove":
            del parent[last]
        else:
            parent[last] = copy.deepcopy(op["value"])
    elif isinstance(parent, list):
        if kind == "add":
            parent.insert(_index(parent, last, allow_end=True), copy.deepcopy(op["value"]))
        elif kind == "remove":
            del parent[_index(parent, last, allow_end=False)]
        else:
            parent[_index(parent, last, allow_end=False)] = copy.deepcopy(op["value"])
    else:
        raise PatchError(f"cannot patch into {type(parent).__name__}")
    return doc


def apply(doc: Any, ops: Iterable[Op]) -> Any:
    for op in ops:
        doc = apply_op(doc, op)
    return doc


def get(doc: Any, path: str, default: Any = None) -> Any:
    node = doc
    for token in split(path):
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return default
    return node
