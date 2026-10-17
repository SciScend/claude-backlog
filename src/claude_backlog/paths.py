"""Where claude-backlog keeps its data."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["HOME_ENV", "data_dir", "db_path", "task_dir", "worktree_dir", "reports_dir"]

HOME_ENV = "CLAUDE_BACKLOG_HOME"


def data_dir() -> Path:
    """``$CLAUDE_BACKLOG_HOME``, else ``$XDG_DATA_HOME/claude-backlog``."""
    explicit = os.environ.get(HOME_ENV)
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "claude-backlog"


def db_path() -> Path:
    return data_dir() / "backlog.db"


def task_dir(task_id: str) -> Path:
    """The task's own files: prompt, STATE.md, SUMMARY.md, logs."""
    return data_dir() / "tasks" / task_id


def worktree_dir(task_id: str) -> Path:
    return data_dir() / "worktrees" / task_id


def reports_dir() -> Path:
    return data_dir() / "reports"
