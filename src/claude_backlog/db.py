"""SQLite storage: schema, migrations and a change counter.

Every process (the service, the runner thread, the CLI) opens its own
connection. WAL mode lets them read and write side by side. Each write bumps
``meta.revision``, which the board watches to know when to refresh.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from claude_backlog import paths

__all__ = ["SCHEMA_VERSION", "connect", "transaction", "revision", "now"]

SCHEMA_VERSION = 1

MIGRATIONS = {
    1: """
    CREATE TABLE meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version INTEGER NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0,
        next_task_number INTEGER NOT NULL DEFAULT 1
    );
    INSERT INTO meta (id, schema_version) VALUES (1, 1);

    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        prompt TEXT NOT NULL,
        project_dir TEXT NOT NULL,
        repo_root TEXT,
        subdir TEXT,
        base_branch TEXT,
        base_commit TEXT,
        branch TEXT,
        worktree TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        priority INTEGER NOT NULL DEFAULT 2,
        position REAL NOT NULL DEFAULT 0,
        model TEXT,
        effort TEXT,
        max_windows TEXT,
        threshold REAL,
        max_attempts INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        finished_at REAL,
        seen_at REAL
    );
    CREATE INDEX tasks_status ON tasks (status, priority, position);

    CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
        started_at REAL NOT NULL,
        ended_at REAL,
        exit_code INTEGER,
        result TEXT,
        log_path TEXT,
        trigger TEXT
    );
    CREATE INDEX runs_task ON runs (task_id, started_at);

    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
}


def now() -> float:
    return time.time()


def _migrate(conn: sqlite3.Connection) -> None:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    current = 0
    if has_meta:
        current = conn.execute("SELECT schema_version FROM meta").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"the backlog database has schema {current}, newer than this version "
            f"of claude-backlog understands ({SCHEMA_VERSION}); upgrade claude-backlog"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with conn:
            conn.executescript(MIGRATIONS[version])
            conn.execute("UPDATE meta SET schema_version = ?", (version,))


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database, creating and migrating it when needed."""
    path = Path(path) if path else paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    _migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """A write transaction that also bumps the change counter."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("UPDATE meta SET revision = revision + 1")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def revision(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT revision FROM meta").fetchone()[0]
