"""Structural error-prevention guards for smaller models (structural checks).

Catch syntax errors, destructive file truncations, invalid JSON, and edit-string
mismatches in milliseconds instead of waiting for a 10-minute test suite to fail.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from . import worktrees as wt

PROTECTED_EXACT = frozenset({
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
})
PROTECTED_GLOBS = ("jest-*.json", "tsconfig.*.json")


def is_protected(path: Path | str) -> bool:
    name = Path(path).name
    if name in PROTECTED_EXACT:
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in PROTECTED_GLOBS)


def _balanced_delimiters(text: str) -> str | None:
    """Fast lexical delimiter balance check for JS/TS files, ignoring strings and comments."""
    pairs = {"{": "}", "[": "]", "(": ")"}
    closing = {"}": "{", "]": "[", ")": "("}
    stack: list[tuple[str, int]] = []
    line = 1
    i = 0
    n = len(text)
    state = "code"
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
            if state == "line_comment":
                state = "code"
            i += 1
            continue
        if state == "line_comment":
            i += 1
            continue
        if state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
            else:
                i += 1
            continue
        if state in ("single", "double", "template"):
            if ch == "\\":
                i += 2
                continue
            if (state == "single" and ch == "'") or (state == "double" and ch == '"') or (state == "template" and ch == "`"):
                state = "code"
            i += 1
            continue
        if ch == "/" and nxt == "/":
            state = "line_comment"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            state = "block_comment"
            i += 2
            continue
        if ch == "'":
            state = "single"
            i += 1
            continue
        if ch == '"':
            state = "double"
            i += 1
            continue
        if ch == "`":
            state = "template"
            i += 1
            continue
        if ch in pairs:
            stack.append((ch, line))
        elif ch in closing:
            if not stack:
                return f"unmatched '{ch}' at line {line}"
            top, top_line = stack.pop()
            if top != closing[ch]:
                return f"mismatched '{ch}' at line {line} (opened '{top}' at line {top_line})"
        i += 1
    if stack:
        top, top_line = stack[-1]
        return f"unclosed '{top}' opened at line {top_line}"
    return None


def check_syntax(path: Path) -> str | None:
    """Return None when `path` passes a fast syntax check, or a concise error string."""
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    if suffix not in (".json", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx"):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"{path.name}: unreadable ({exc})"
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return f"{path.name}: invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}"
        return None
    if suffix == ".py":
        try:
            ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            return f"{path.name}: Python syntax error at line {exc.lineno}: {exc.msg}"
        return None
    if suffix in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")
        if node:
            proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
            if proc.returncode != 0:
                first = (proc.stderr or proc.stdout).strip().splitlines()[:3]
                return f"{path.name}: JS syntax error: {' | '.join(first)}"
            return None
        err = _balanced_delimiters(text)
        return f"{path.name}: {err}" if err else None
    if suffix in (".ts", ".tsx"):
        err = _balanced_delimiters(text)
        return f"{path.name}: {err}" if err else None
    return None


def guard_write(target: Path, new_content: str, *, max_shrink_ratio: float = 0.5,
                min_existing_chars: int = 500) -> str | None:
    """Refuse invalid JSON or destructive truncation before writing `target`."""
    if target.suffix.lower() == ".json":
        try:
            json.loads(new_content)
        except json.JSONDecodeError as exc:
            return f"ERROR: Refused writing invalid JSON to {target.name} (line {exc.lineno}: {exc.msg})"
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = ""
        protected = is_protected(target)
        threshold = 1 if protected else min_existing_chars
        if len(existing) >= threshold and len(new_content) < len(existing) * max_shrink_ratio:
            return (f"ERROR: Refused destructive truncation of {target.name} "
                    f"({len(existing)} -> {len(new_content)} chars; floor {int(len(existing) * max_shrink_ratio)})")
    return None


def safe_edit_hint(content: str, old_text: str, filename: str = "file") -> str:
    """Return a targeted hint for `edit_file` failures so smaller models fix mismatches in one turn."""
    if not old_text:
        return "ERROR: old_text cannot be empty."
    count = content.count(old_text)
    if count == 1:
        return "OK"
    if count > 1:
        lines: list[int] = []
        start = 0
        while True:
            idx = content.find(old_text, start)
            if idx < 0:
                break
            lines.append(content.count("\n", 0, idx) + 1)
            start = idx + 1
        return (f"ERROR: old_text matched {count} times in {filename} "
                f"(lines {', '.join(str(n) for n in lines[:10])}). Include 2-3 surrounding lines.")
    content_lines = content.splitlines()
    needle_lines = [ln.strip() for ln in old_text.splitlines() if ln.strip()]
    if not content_lines or not needle_lines:
        return f"ERROR: old_text not found verbatim in {filename}."
    first_needle = needle_lines[0]
    best_idx, best_score = 0, -1.0
    for idx, line in enumerate(content_lines):
        score = difflib.SequenceMatcher(None, first_needle, line.strip()).ratio()
        if score > best_score:
            best_score, best_idx = score, idx
    s0 = max(0, best_idx - 2)
    s1 = min(len(content_lines), best_idx + max(4, len(needle_lines) + 2))
    ctx = "\n".join(f"{n}: {content_lines[n - 1]}" for n in range(s0 + 1, s1 + 1))
    return f"ERROR: old_text not found verbatim in {filename}. Closest region (lines {s0 + 1}-{s1}):\n{ctx}"


def validate_worktree_edits(worktree: Path, owns: Iterable[str] = ()) -> list[str]:
    """Validate ownership and fast syntax across all dirty paths in `worktree`."""
    errors: list[str] = []
    owns_list = [p for p in owns if isinstance(p, str) and p.strip()]
    dirty = wt.dirty_paths(worktree)
    restored = False
    for rel in list(dirty):
        if (rel == ".yarn/install-state.gz" or rel.endswith("/.yarn/install-state.gz")) \
                and (not owns_list or not wt.path_owned(rel, owns_list)):
            wt.git(worktree, "checkout", "--", rel, check=False)
            restored = True
    if restored:
        dirty = wt.dirty_paths(worktree)
    if owns_list:
        unowned = [p for p in dirty if not wt.path_owned(p, owns_list)]
        if unowned:
            errors.append(f"modified unowned path {', '.join(unowned)}")
    for rel in dirty:
        syntax_err = check_syntax(worktree / rel)
        if syntax_err:
            errors.append(syntax_err)
    return errors
