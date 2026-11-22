"""Merge job record. The CI service lands every merge; this module holds no landing code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MergeJob:
    task: str
    branch: str
    level: str
    worktree: Path
    target_branch: str | None = None
