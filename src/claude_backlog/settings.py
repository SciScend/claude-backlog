"""User settings, stored as JSON values in the ``settings`` table."""

from __future__ import annotations

import json
import os
import sqlite3

from claude_backlog import db

__all__ = ["DEFAULTS", "PROMPT_TEMPLATE", "get", "get_all", "update"]

PROMPT_TEMPLATE = """\
## Goal
<What should be true when this is done, and why it matters.>

## Context
<Where to start: files, docs, error messages, code to follow as a pattern, how to run it.>

## Constraints
<What must not change and what is out of scope: files, APIs, behavior, new dependencies.>

## Done when
<Observable results, each with the exact command or step that checks it.>

## Milestones
<For a long task: steps in order, each one checkable on its own. Delete for a short task.>

## When unsure
<Choices Claude may make alone, choices to leave for you, what to do if blocked.>
"""

DEFAULTS = {
    # Stop at this share of the weekly limit (passed to nightshift as -t).
    "threshold": 50.0,
    # Start the queue when the screensaver has been on this long.
    "away_enabled": True,
    "away_minutes": 15,
    # When I come back to a run that started because I was away:
    # "stop" (stop now) or "finish" (let the current task finish).
    "on_return": "stop",
    # Task defaults; empty means Claude's own default.
    "model": "",
    "effort": "",
    "max_windows": "2",
    "max_attempts": 3,
    # Writing a task's prompt with Claude, separate from the task model (which
    # is for runs only). Empty means Claude Code's own default.
    "draft_model": "sonnet",
    "draft_effort": "low",
    # Folders whose subfolders the new-task form offers as projects.
    "project_roots": [os.path.expanduser("~")],
    # What the new-task form puts in the prompt field. The lines in angle
    # brackets are hints; the form does not accept them unchanged.
    "prompt_template": PROMPT_TEMPLATE,
    # Phone access through tailscale serve: the Tailscale login allowed in.
    "tailscale_login": "",
    "port": 8765,
}

CHOICES = {
    "on_return": {"stop", "finish"},
    "effort": {"", "low", "medium", "high", "xhigh", "max"},
    "draft_model": {"", "sonnet", "haiku", "opus"},
    "draft_effort": {"", "low", "medium", "high", "xhigh", "max"},
}


def _validate(key, value):
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key!r}")
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
    elif isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        if key == "threshold" and not 1 <= value <= 100:
            raise ValueError("threshold must be between 1 and 100")
        if key in ("away_minutes", "max_attempts") and value < 1:
            raise ValueError(f"{key} must be at least 1")
        value = type(default)(value)
    elif isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{key} must be a list of folders")
    else:
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        value = value.strip()
        if key == "max_windows" and value and value != "auto" and not value.isdigit():
            raise ValueError("max_windows must be a number or 'auto'")
    if key in CHOICES and value not in CHOICES[key]:
        raise ValueError(f"{key} must be one of {sorted(CHOICES[key])}")
    return value


def get_all(conn: sqlite3.Connection) -> dict:
    values = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in DEFAULTS:
            values[row["key"]] = json.loads(row["value"])
    return values


def get(conn: sqlite3.Connection, key: str):
    return get_all(conn)[key]


def update(conn: sqlite3.Connection, changes: dict) -> dict:
    """Validate and store ``changes``; returns all settings."""
    clean = {key: _validate(key, value) for key, value in changes.items()}
    with db.transaction(conn):
        for key, value in clean.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
    return get_all(conn)
