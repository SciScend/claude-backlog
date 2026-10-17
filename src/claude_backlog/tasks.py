"""Tasks: creating, editing, ordering, and the task file format."""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from pathlib import Path

from claude_backlog import db, gitops, paths

__all__ = [
    "STATUSES",
    "PRIORITIES",
    "TaskError",
    "create",
    "get",
    "list_tasks",
    "queue",
    "next_queued",
    "update",
    "move",
    "set_status",
    "delete",
    "parse_task_file",
    "format_task_file",
    "slugify",
]

STATUSES = ("hold", "queued", "running", "done", "attention", "archived")
PRIORITIES = {"high": 1, "normal": 2, "low": 3}
PRIORITY_NAMES = {v: k for k, v in PRIORITIES.items()}

# Fields that can be changed while a task is not running.
EDITABLE = {"title", "prompt", "priority", "model", "effort", "max_windows",
            "threshold", "max_attempts", "project_dir", "base_branch"}
# Fields fixed once the task has started, because its branch depends on them.
FIXED_AFTER_START = {"project_dir", "base_branch"}

_CYRILLIC = dict(zip(
    "абвгдежзийклмнопрстуфхцчшщъьюяыэё",
    ["a", "b", "v", "g", "d", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
     "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sht", "a", "y", "yu", "ya", "y",
     "e", "yo"],
    strict=True,
))


class TaskError(ValueError):
    pass


def slugify(text: str, limit: int = 32) -> str:
    text = "".join(_CYRILLIC.get(c, c) for c in text.lower())
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:limit].rstrip("-") or "task"


def _priority(value) -> int:
    if isinstance(value, str):
        if value.lower() in PRIORITIES:
            return PRIORITIES[value.lower()]
        if value.isdigit():
            value = int(value)
    if value in (1, 2, 3):
        return value
    raise TaskError("priority must be high, normal or low")


def _clean_max_windows(value):
    if value in (None, ""):
        return None
    value = str(value).strip()
    if value != "auto" and not (value.isdigit() and int(value) > 0):
        raise TaskError("max windows must be a positive number or 'auto'")
    return value


def to_dict(row: sqlite3.Row) -> dict:
    task = dict(row)
    task["priority_name"] = PRIORITY_NAMES[task["priority"]]
    task["project_name"] = Path(task["project_dir"]).name
    task["in_git"] = bool(task["repo_root"])
    return task


def get(conn, task_id) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskError(f"no task {task_id!r}")
    return to_dict(row)


def list_tasks(conn, include_archived=False) -> list[dict]:
    sql = "SELECT * FROM tasks"
    if not include_archived:
        sql += " WHERE status != 'archived'"
    sql += " ORDER BY priority, position, created_at"
    return [to_dict(r) for r in conn.execute(sql)]


def queue(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'queued' "
        "ORDER BY priority, position, created_at"
    )
    return [to_dict(r) for r in rows]


def next_queued(conn) -> dict | None:
    q = queue(conn)
    return q[0] if q else None


def _end_position(conn, status, priority) -> float:
    row = conn.execute(
        "SELECT MAX(position) FROM tasks WHERE status = ? AND priority = ?",
        (status, priority),
    ).fetchone()
    return (row[0] if row[0] is not None else 0.0) + 1.0


def _new_id(conn, title) -> str:
    number = conn.execute("SELECT next_task_number FROM meta").fetchone()[0]
    conn.execute("UPDATE meta SET next_task_number = next_task_number + 1")
    return f"{slugify(title)}-{number}"


def _title_from_prompt(prompt: str) -> str:
    for line in prompt.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return "Untitled task"


def create(conn, prompt, project_dir, title=None, priority="normal", status="queued",
           model=None, effort=None, max_windows=None, threshold=None,
           max_attempts=None, base_branch=None) -> dict:
    prompt = (prompt or "").strip()
    if not prompt:
        raise TaskError("the prompt is empty")
    if status not in ("queued", "hold"):
        raise TaskError("a new task goes to the queue or on hold")
    project = Path(os.path.expanduser(str(project_dir or ""))).resolve()
    if not project_dir or not project.is_dir():
        raise TaskError(f"project folder {project_dir!r} does not exist")
    title = (title or "").strip() or _title_from_prompt(prompt)
    prio = _priority(priority)
    info = gitops.repo_info(project)
    if info and base_branch is None:
        base_branch = info.branch or "HEAD"
    now = db.now()
    with db.transaction(conn):
        task_id = _new_id(conn, title)
        conn.execute(
            """INSERT INTO tasks (id, title, prompt, project_dir, repo_root, subdir,
                   base_branch, status, priority, position, model, effort,
                   max_windows, threshold, max_attempts, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, title, prompt, str(project),
             info.root if info else None, info.subdir if info else None,
             base_branch if info else None, status, prio,
             _end_position(conn, status, prio), model or None, effort or None,
             _clean_max_windows(max_windows), threshold, max_attempts, now, now),
        )
    return get(conn, task_id)


def update(conn, task_id, changes: dict) -> dict:
    task = get(conn, task_id)
    unknown = set(changes) - EDITABLE
    if unknown:
        raise TaskError(f"cannot change {', '.join(sorted(unknown))}")
    if task["status"] == "running":
        raise TaskError("the task is running; stop it before editing")
    if task["attempts"] and FIXED_AFTER_START & set(changes):
        changed = {k for k in FIXED_AFTER_START & set(changes) if changes[k] != task[k]}
        if changed:
            raise TaskError("the project and base branch are fixed once a task has run")
    clean = {}
    for key, value in changes.items():
        if key == "priority":
            value = _priority(value)
        elif key == "max_windows":
            value = _clean_max_windows(value)
        elif key in ("title", "prompt"):
            value = (value or "").strip()
            if not value:
                raise TaskError(f"the {key} is empty")
        elif key == "project_dir":
            project = Path(os.path.expanduser(value)).resolve()
            if not project.is_dir():
                raise TaskError(f"project folder {value!r} does not exist")
            info = gitops.repo_info(project)
            value = str(project)
            clean["repo_root"] = info.root if info else None
            clean["subdir"] = info.subdir if info else None
            if "base_branch" not in changes:
                clean["base_branch"] = (info.branch or "HEAD") if info else None
        elif key == "threshold":
            value = None if value in (None, "") else float(value)
            if value is not None and not 1 <= value <= 100:
                raise TaskError("threshold must be between 1 and 100")
        elif key == "max_attempts":
            value = None if value in (None, "") else int(value)
        elif value == "":
            value = None
        clean[key] = value
    if "priority" in clean and clean["priority"] != task["priority"]:
        clean["position"] = _end_position(conn, task["status"], clean["priority"])
    if not clean:
        return task
    clean["updated_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in clean)
    with db.transaction(conn):
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*clean.values(), task_id))
    return get(conn, task_id)


def move(conn, task_id, status, priority=None, index=None) -> dict:
    """Put a task into ``status`` (and priority group) at ``index`` (None: the end).

    This is what dragging a card does. Only the moved task's position changes:
    it gets a value between its new neighbours.
    """
    task = get(conn, task_id)
    if status not in ("hold", "queued"):
        raise TaskError("tasks can only be moved to the queue or on hold")
    if task["status"] == "running":
        raise TaskError("the task is running; stop it first")
    prio = task["priority"] if priority is None else _priority(priority)
    siblings = [
        r["position"]
        for r in conn.execute(
            "SELECT position FROM tasks WHERE status = ? AND priority = ? AND id != ? "
            "ORDER BY position, created_at",
            (status, prio, task_id),
        )
    ]
    if index is None or index >= len(siblings):
        position = (siblings[-1] + 1.0) if siblings else 1.0
    elif index <= 0:
        position = siblings[0] - 1.0 if siblings else 1.0
    else:
        position = (siblings[index - 1] + siblings[index]) / 2.0
    fields = {"status": status, "priority": prio, "position": position,
              "updated_at": db.now()}
    if task["status"] in ("done", "attention") and status == "queued":
        # Running a finished task again: a fresh round of attempts.
        fields["attempts"] = 0
        fields["finished_at"] = None
        fields["note"] = None
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction(conn):
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
    return get(conn, task_id)


def set_status(conn, task_id, status, **fields) -> dict:
    if status not in STATUSES:
        raise TaskError(f"unknown status {status!r}")
    fields = {"status": status, "updated_at": db.now(), **fields}
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction(conn):
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
    return get(conn, task_id)


def mark_seen(conn, task_id) -> None:
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET seen_at = ? WHERE id = ?", (db.now(), task_id))


def delete(conn, task_id) -> None:
    task = get(conn, task_id)
    if task["status"] == "running":
        raise TaskError("the task is running; stop it first")
    with db.transaction(conn):
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


# --- task files ---------------------------------------------------------------

FILE_FIELDS = ("title", "project", "priority", "status", "model", "effort",
               "max-windows", "threshold", "max-attempts", "base-branch")


def parse_task_file(text: str, default_project=None) -> dict:
    """Parse a task file: optional ``---`` front matter, then the prompt.

    Returns keyword arguments for :func:`create`.
    """
    meta = {}
    body = text
    if text.startswith("---"):
        lines = text.splitlines()
        try:
            end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
        except StopIteration:
            raise TaskError("the front matter has no closing '---'") from None
        for line in lines[1:end]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if ":" not in line:
                raise TaskError(f"cannot read front matter line {line!r}")
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key == "workdir":  # the name used by the first design
                key = "project"
            if key not in FILE_FIELDS:
                raise TaskError(f"unknown field {key!r} in the front matter")
            meta[key] = value.strip().strip("'\"")
        body = "\n".join(lines[end + 1:])
    kwargs = {"prompt": body.strip(), "project_dir": meta.get("project") or default_project}
    if "title" in meta:
        kwargs["title"] = meta["title"]
    if "priority" in meta:
        kwargs["priority"] = meta["priority"]
    if meta.get("status") == "hold":
        kwargs["status"] = "hold"
    for src, dst in (("model", "model"), ("effort", "effort"),
                     ("max-windows", "max_windows"), ("base-branch", "base_branch")):
        if meta.get(src):
            kwargs[dst] = meta[src]
    if meta.get("threshold"):
        kwargs["threshold"] = float(meta["threshold"])
    if meta.get("max-attempts"):
        kwargs["max_attempts"] = int(meta["max-attempts"])
    return kwargs


def format_task_file(task: dict) -> str:
    home = os.path.expanduser("~")
    project = task["project_dir"]
    if project == home or project.startswith(home + os.sep):
        project = "~" + project[len(home):]
    lines = ["---", f"title: {task['title']}", f"project: {project}",
             f"priority: {PRIORITY_NAMES[task['priority']]}"]
    if task["status"] == "hold":
        lines.append("status: hold")
    for key, field in (("model", "model"), ("effort", "effort"),
                       ("max-windows", "max_windows"), ("threshold", "threshold"),
                       ("max-attempts", "max_attempts"), ("base-branch", "base_branch")):
        if task.get(field) not in (None, ""):
            lines.append(f"{key}: {task[field]}")
    lines += ["---", task["prompt"], ""]
    return "\n".join(lines)


def task_files(task_id) -> Path:
    return paths.task_dir(task_id)
