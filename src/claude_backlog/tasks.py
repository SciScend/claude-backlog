"""Tasks: creating, editing, ordering, and the task file format."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

from claude_backlog import db, gitops, paths, settings

__all__ = [
    "STATUSES",
    "PRIORITIES",
    "TaskError",
    "create",
    "get",
    "list_tasks",
    "queue",
    "runnable",
    "next_queued",
    "waits_for",
    "update",
    "move",
    "set_status",
    "start_draft",
    "finish_draft",
    "fail_draft",
    "due_drafts",
    "answer",
    "open_blocking",
    "with_answers",
    "delete",
    "parse_task_file",
    "format_task_file",
    "slugify",
]

# "drafting": Claude is writing the prompt; the task never runs in this state.
STATUSES = ("drafting", "hold", "queued", "running", "done", "attention", "archived")
PRIORITIES = {"high": 1, "normal": 2, "low": 3}
PRIORITY_NAMES = {v: k for k, v in PRIORITIES.items()}

# Fields that can be changed while a task is not running.
EDITABLE = {"title", "prompt", "priority", "model", "effort", "max_windows", "spend",
            "threshold", "max_attempts", "project_dir", "base_branch", "after_task"}
# Fields fixed once the task has started, because its branch depends on them.
FIXED_AFTER_START = {"project_dir", "base_branch", "after_task"}
# A task that starts after another one may start once that one is in here.
FINISHED = ("done", "archived")
# Where the user's answers to Claude's questions go in the prompt.
ANSWERS_HEADING = "## Answers from the user"

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


def _clean_spend(value):
    if value in (None, ""):
        return None
    value = str(value).strip().rstrip("%").strip()
    if value == "auto":
        return value
    try:
        pct = float(value)
    except ValueError:
        pct = 0
    if not 0 < pct <= 100:
        raise TaskError("the budget must be a percentage above 0 and at most 100, "
                        "or 'auto'")
    return f"{pct:g}"


def to_dict(row: sqlite3.Row) -> dict:
    task = dict(row)
    task["questions"] = json.loads(task["questions"]) if task["questions"] else []
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


def waits_for(conn, task) -> dict | None:
    """The unfinished task that ``task`` starts after, or None if it may start."""
    if not task.get("after_task"):
        return None
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task["after_task"],)).fetchone()
    if row is None or row["status"] in FINISHED:
        return None
    return to_dict(row)


def runnable(conn) -> list[dict]:
    """The queue without the tasks that wait for another task."""
    return [t for t in queue(conn) if waits_for(conn, t) is None]


def next_queued(conn) -> dict | None:
    q = runnable(conn)
    return q[0] if q else None


def _clean_after(conn, after_id, task_id, repo_root, project_dir):
    """Check the task to start after: it exists, is in the same repository
    (or folder) and does not lead back to this task."""
    if not after_id:
        return None
    after_id = str(after_id).strip()
    if after_id == task_id:
        raise TaskError("a task cannot start after itself")
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (after_id,)).fetchone()
    if row is None:
        raise TaskError(f"no task {after_id!r} to start after")
    same = (row["repo_root"] == repo_root) if repo_root else (
        not row["repo_root"] and row["project_dir"] == project_dir)
    if not same:
        raise TaskError("a task can only start after a task of the same repository")
    seen = {after_id}
    while row["after_task"]:
        if row["after_task"] == task_id:
            raise TaskError("these tasks would wait for each other")
        if row["after_task"] in seen:
            break
        seen.add(row["after_task"])
        row = conn.execute("SELECT * FROM tasks WHERE id = ?",
                           (row["after_task"],)).fetchone()
        if row is None:
            break
    return after_id


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


def _headings(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if line.strip().startswith("#")}


def _title_from_prompt(prompt: str, skip=frozenset()) -> str:
    """The first line of the prompt, leaving out the template's own headings."""
    for line in prompt.splitlines():
        if line.strip() in skip:
            continue
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return "Untitled task"


def create(conn, prompt=None, project_dir=None, title=None, priority="normal",
           status="queued", model=None, effort=None, max_windows=None, spend=None,
           threshold=None,
           max_attempts=None, base_branch=None, after_task=None,
           description=None, merge_ref=None) -> dict:
    """A new task. Without a prompt but with a description, the task is
    "drafting" until Claude has written its prompt (see start_draft), and
    ``status`` is where it goes then. ``merge_ref`` is set for a task that
    resolves a merge conflict: the branch it merges into its own."""
    prompt = (prompt or "").strip()
    description = (description or "").strip() or None
    if not prompt and not description:
        raise TaskError("the prompt is empty")
    if status not in ("queued", "hold"):
        raise TaskError("a new task goes to the queue or on hold")
    project = Path(os.path.expanduser(str(project_dir or ""))).resolve()
    if not project_dir or not project.is_dir():
        raise TaskError(f"project folder {project_dir!r} does not exist")
    skip = _headings(settings.get(conn, "prompt_template"))
    title = (title or "").strip() or _title_from_prompt(prompt or description, skip)
    draft_target = None
    if not prompt:
        draft_target, status = status, "drafting"
    prio = _priority(priority)
    info = gitops.repo_info(project)
    if info and base_branch is None:
        base_branch = info.branch or "HEAD"
    after_task = _clean_after(conn, after_task, None, info.root if info else None,
                              str(project))
    now = db.now()
    with db.transaction(conn):
        task_id = _new_id(conn, title)
        conn.execute(
            """INSERT INTO tasks (id, title, prompt, project_dir, repo_root, subdir,
                   base_branch, after_task, status, priority, position, model, effort,
                   max_windows, spend, threshold, max_attempts, description,
                   draft_target, merge_ref, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, title, prompt, str(project),
             info.root if info else None, info.subdir if info else None,
             base_branch if info else None, after_task, status, prio,
             _end_position(conn, status, prio), model or None, effort or None,
             _clean_max_windows(max_windows), _clean_spend(spend), threshold,
             max_attempts, description,
             draft_target, merge_ref if info else None, now, now),
        )
    return get(conn, task_id)


def update(conn, task_id, changes: dict) -> dict:
    task = get(conn, task_id)
    unknown = set(changes) - EDITABLE
    if unknown:
        raise TaskError(f"cannot change {', '.join(sorted(unknown))}")
    if task["status"] == "running":
        raise TaskError("the task is running; stop it before editing")
    if task["status"] == "drafting" and {"title", "prompt"} & set(changes):
        raise TaskError("Claude is still writing the prompt; edit it once the draft "
                        "is done")
    if task["attempts"] and FIXED_AFTER_START & set(changes):
        changed = {k for k in FIXED_AFTER_START & set(changes)
                   if (changes[k] or None) != task[k]}
        if changed:
            raise TaskError("the project, base branch and 'start after' are fixed once "
                            "a task has run")
    clean = {}
    for key, value in changes.items():
        if key == "priority":
            value = _priority(value)
        elif key == "max_windows":
            value = _clean_max_windows(value)
        elif key == "spend":
            value = _clean_spend(value)
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
        elif key == "after_task":
            continue  # checked below, once the project is known
        elif value == "":
            value = None
        clean[key] = value
    if "after_task" in changes or ("project_dir" in clean and task["after_task"]):
        clean["after_task"] = _clean_after(
            conn, changes.get("after_task", task["after_task"]), task_id,
            clean.get("repo_root", task["repo_root"]),
            clean.get("project_dir", task["project_dir"]))
    if "priority" in clean and clean["priority"] != task["priority"]:
        clean["position"] = _end_position(conn, task["status"], clean["priority"])
    if task["draft_error"] and clean.get("prompt"):
        clean["draft_error"] = None  # written by hand instead
        clean["draft_retry_at"] = None
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
    if task["status"] == "drafting":
        raise TaskError("Claude is still writing the prompt; wait for the draft")
    if status == "queued" and not task["prompt"].strip():
        raise TaskError("the task has no prompt yet: retry the draft or write it")
    if status == "queued" and open_blocking(task):
        raise TaskError("answer the questions that block this task first")
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


def _write(conn, task_id, fields, where="") -> bool:
    """Set ``fields`` on the task; False when ``where`` did not match."""
    fields = {**fields, "updated_at": db.now()}
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction(conn):
        cur = conn.execute(f"UPDATE tasks SET {sets} WHERE id = ? {where}",
                           (*fields.values(), task_id))
    return cur.rowcount > 0


def start_draft(conn, task_id) -> dict:
    """Have Claude write the prompt again after a draft failed."""
    task = get(conn, task_id)
    if not task["description"]:
        raise TaskError("the task has no description to write the prompt from")
    if task["status"] != "hold" or not task["draft_error"]:
        raise TaskError("only a task whose draft failed can be drafted again")
    _write(conn, task_id, {"status": "drafting", "draft_error": None,
                           "draft_retry_at": None,
                           "draft_target": task["draft_target"] or "queued"})
    return get(conn, task_id)


def finish_draft(conn, task_id, result: dict) -> dict | None:
    """Fill in Claude's draft. The task goes on hold when a question blocks
    it, and where the user chose otherwise. None when the task was no longer
    drafting (deleted or archived meanwhile)."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ? AND status = 'drafting'",
                       (task_id,)).fetchone()
    if row is None:
        return None
    task = to_dict(row)
    questions = [{**q, "answer": ""} for q in result.get("questions") or []]
    status = task["draft_target"] if task["draft_target"] in ("queued", "hold") \
        else "queued"
    if any(q["blocking"] for q in questions):
        status = "hold"
    fields = {"prompt": result["prompt"].strip(), "questions": json.dumps(questions),
              "status": status, "draft_error": None,
              "position": _end_position(conn, status, task["priority"])}
    if (result.get("title") or "").strip():
        fields["title"] = result["title"].strip()[:80]
    if not _write(conn, task_id, fields, "AND status = 'drafting'"):
        return None
    return get(conn, task_id)


def fail_draft(conn, task_id, error: str, retry_at: float | None = None) -> dict | None:
    """The draft failed: the task goes on hold with the error. With
    ``retry_at`` (a usage limit stopped it) it is drafted again by itself then."""
    row = conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    fields = {"status": "hold", "draft_error": error or "the draft failed",
              "draft_retry_at": retry_at,
              "position": _end_position(conn, "hold", row["priority"])}
    if not _write(conn, task_id, fields, "AND status = 'drafting'"):
        return None
    return get(conn, task_id)


def due_drafts(conn, now: float | None = None) -> list[str]:
    """The tasks whose failed draft is due to be tried again by itself."""
    now = db.now() if now is None else now
    return [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE status = 'hold' AND draft_error IS NOT NULL "
        "AND draft_retry_at IS NOT NULL AND draft_retry_at <= ? ORDER BY draft_retry_at",
        (now,))]


def open_blocking(task) -> list[dict]:
    """The blocking questions that have no answer yet."""
    return [q for q in task["questions"]
            if q.get("blocking") and not (q.get("answer") or "").strip()]


def with_answers(prompt: str, questions: list[dict]) -> str:
    """The prompt with an "Answers from the user" section for the answered
    questions, in place of an earlier one (none when nothing is answered)."""
    lines = prompt.strip().splitlines()
    if ANSWERS_HEADING in (line.strip() for line in lines):
        start = next(i for i, line in enumerate(lines) if line.strip() == ANSWERS_HEADING)
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("## ")), len(lines))
        lines = lines[:start] + lines[end:]
    text = "\n".join(lines).strip()
    answered = [q for q in questions if (q.get("answer") or "").strip()]
    if not answered:
        return text
    section = [ANSWERS_HEADING, "",
               "These answers replace what the prompt above assumes about the same "
               "questions.", ""]
    for q in answered:
        answer = "\n  ".join(q["answer"].strip().splitlines())
        section += [f"- {q['question']}", f"  Answer: {answer}"]
    return text + "\n\n" + "\n".join(section)


def answer(conn, task_id, answers: dict) -> dict:
    """Record answers to Claude's questions (by number; empty clears one) and
    put them into the prompt. No new call to Claude."""
    task = get(conn, task_id)
    if task["status"] in ("running", "drafting"):
        raise TaskError("the task is running; stop it first" if task["status"] == "running"
                        else "Claude is still writing the prompt")
    questions = task["questions"]
    for key, value in answers.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise TaskError(f"no question {key!r}") from None
        if not 0 <= index < len(questions):
            raise TaskError(f"no question {key!r}")
        questions[index]["answer"] = str(value or "").strip()
    _write(conn, task_id, {"questions": json.dumps(questions),
                           "prompt": with_answers(task["prompt"], questions)})
    return get(conn, task_id)


def mark_seen(conn, task_id) -> None:
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET seen_at = ? WHERE id = ?", (db.now(), task_id))


def delete(conn, task_id) -> None:
    task = get(conn, task_id)
    if task["status"] == "running":
        raise TaskError("the task is running; stop it first")
    with db.transaction(conn):
        # Tasks that waited for this one start from their base branch instead.
        conn.execute("UPDATE tasks SET after_task = NULL WHERE after_task = ?",
                     (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


# --- task files ---------------------------------------------------------------

FILE_FIELDS = ("title", "project", "priority", "status", "model", "effort",
               "max-windows", "spend", "threshold", "max-attempts", "base-branch",
               "after")


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
                     ("max-windows", "max_windows"), ("spend", "spend"),
                     ("base-branch", "base_branch"), ("after", "after_task")):
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
                       ("max-windows", "max_windows"), ("spend", "spend"),
                       ("threshold", "threshold"),
                       ("max-attempts", "max_attempts"), ("base-branch", "base_branch"),
                       ("after", "after_task")):
        if task.get(field) not in (None, ""):
            lines.append(f"{key}: {task[field]}")
    lines += ["---", task["prompt"], ""]
    return "\n".join(lines)


def task_files(task_id) -> Path:
    return paths.task_dir(task_id)
