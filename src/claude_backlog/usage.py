"""Current usage for the board, read through ``claude-nightshift --usage-json``.

The board only shows it. Decisions about the weekly limit are nightshift's.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time

__all__ = ["NIGHTSHIFT_ENV", "nightshift_command", "read_runs", "RunsCache", "UsageCache"]

NIGHTSHIFT_ENV = "CLAUDE_BACKLOG_NIGHTSHIFT"


def nightshift_command() -> list[str]:
    """How to run nightshift: ``$CLAUDE_BACKLOG_NIGHTSHIFT``, else the installed module."""
    override = os.environ.get(NIGHTSHIFT_ENV)
    if override:
        return shlex.split(override)
    return [sys.executable, "-m", "claude_nightshift"]


def read_runs(timeout=20) -> dict:
    """What each nightshift run spent of the weekly pool and its budget, from
    ``claude-nightshift --runs-json``: ``{run_id: {spent, cap, active, finished}}``.
    Empty when nightshift cannot tell."""
    try:
        result = subprocess.run([*nightshift_command(), "--runs-json"],
                                capture_output=True, text=True, timeout=timeout)
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    return data if result.returncode == 0 and isinstance(data, dict) else {}


class RunsCache:
    """Reads what the nightshift runs spent at most every ``max_age`` seconds.
    ``version`` changes when the figures do."""

    def __init__(self, max_age=60, reader=None):
        self.max_age = max_age
        self.reader = reader or read_runs
        self.value = {}
        self.read_at = 0.0
        self.version = 0
        self._lock = threading.Lock()

    def get(self) -> dict:
        with self._lock:
            if time.time() - self.read_at > self.max_age:
                self.read_at = time.time()
                value = self.reader()
                if value != self.value:
                    self.value = value
                    self.version += 1
            return self.value


class UsageCache:
    """Reads usage at most every ``max_age`` seconds."""

    def __init__(self, max_age=300):
        self.max_age = max_age
        self.value = None
        self.error = None
        self.read_at = 0.0
        self._lock = threading.Lock()

    def get(self, force=False) -> dict:
        with self._lock:
            if force or time.time() - self.read_at > self.max_age:
                self._read()
            return {"usage": self.value, "error": self.error, "read_at": self.read_at}

    def _read(self) -> None:
        self.read_at = time.time()
        try:
            result = subprocess.run(
                [*nightshift_command(), "--usage-json"],
                capture_output=True, text=True, timeout=60,
            )
            data = json.loads(result.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            self.error = f"could not run claude-nightshift: {exc}"
            return
        if result.returncode == 0 and "weekly_pct" in data:
            self.value = data
            self.error = None
        else:
            fallback = f"claude-nightshift exit {result.returncode}"
            self.error = data.get("error") or result.stderr.strip()[-300:] or fallback
