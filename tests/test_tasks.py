import pytest

from claude_backlog import settings, tasks
from claude_backlog.tasks import TaskError


def test_slugify():
    assert tasks.slugify("Migrate the API client to v2!") == "migrate-the-api-client-to-v2"
    assert tasks.slugify("Поправи тестовете") == "popravi-testovete"
    assert tasks.slugify("!!!") == "task"
    assert len(tasks.slugify("x" * 100)) == 32


def test_create_in_a_git_repo(conn, repo):
    task = tasks.create(conn, "Do the thing\nin detail", repo, priority="high")
    assert task["id"] == "do-the-thing-1"
    assert task["title"] == "Do the thing"
    assert task["status"] == "queued"
    assert task["priority"] == 1
    assert task["repo_root"] == str(repo)
    assert task["base_branch"] == "main"
    assert task["in_git"]


def test_create_in_a_subfolder_records_it(conn, repo):
    (repo / "docs").mkdir()
    task = tasks.create(conn, "Write docs", repo / "docs")
    assert task["repo_root"] == str(repo)
    assert task["subdir"] == "docs"


def test_create_outside_git(conn, plain_dir):
    task = tasks.create(conn, "Tidy up", plain_dir, status="hold")
    assert not task["in_git"]
    assert task["status"] == "hold"
    assert task["base_branch"] is None


@pytest.mark.parametrize("kwargs, message", [
    ({"prompt": "  "}, "empty"),
    ({"project_dir": "/no/such/folder"}, "does not exist"),
    ({"priority": "urgent"}, "priority"),
    ({"status": "done"}, "queue or on hold"),
    ({"max_windows": "lots"}, "max windows"),
])
def test_create_rejects_bad_input(conn, plain_dir, kwargs, message):
    args = {"prompt": "x", "project_dir": plain_dir, **kwargs}
    with pytest.raises(TaskError, match=message):
        tasks.create(conn, **args)


def test_queue_order_is_priority_then_position(conn, plain_dir):
    a = tasks.create(conn, "a", plain_dir, priority="low")
    b = tasks.create(conn, "b", plain_dir)
    c = tasks.create(conn, "c", plain_dir)
    d = tasks.create(conn, "d", plain_dir, priority="high")
    assert [t["id"] for t in tasks.queue(conn)] == [d["id"], b["id"], c["id"], a["id"]]
    assert tasks.next_queued(conn)["id"] == d["id"]


def test_move_within_and_between_groups(conn, plain_dir):
    a = tasks.create(conn, "a", plain_dir)
    b = tasks.create(conn, "b", plain_dir)
    c = tasks.create(conn, "c", plain_dir)
    tasks.move(conn, c["id"], "queued", 2, 0)
    assert [t["title"] for t in tasks.queue(conn)] == ["c", "a", "b"]
    tasks.move(conn, a["id"], "queued", 2, 2)
    assert [t["title"] for t in tasks.queue(conn)] == ["c", "b", "a"]
    tasks.move(conn, b["id"], "queued", 2, 1)
    assert [t["title"] for t in tasks.queue(conn)] == ["c", "b", "a"]
    tasks.move(conn, a["id"], "queued", "high", 0)
    assert [t["title"] for t in tasks.queue(conn)] == ["a", "c", "b"]
    tasks.move(conn, c["id"], "hold")
    assert [t["title"] for t in tasks.queue(conn)] == ["a", "b"]
    assert tasks.get(conn, c["id"])["status"] == "hold"


def test_moving_a_finished_task_back_resets_its_attempts(conn, plain_dir):
    t = tasks.create(conn, "a", plain_dir)
    tasks.set_status(conn, t["id"], "attention", attempts=3, note="failed",
                     finished_at=1.0)
    moved = tasks.move(conn, t["id"], "queued")
    assert moved["attempts"] == 0
    assert moved["note"] is None
    assert moved["finished_at"] is None


def test_cannot_move_or_edit_a_running_task(conn, plain_dir):
    t = tasks.create(conn, "a", plain_dir)
    tasks.set_status(conn, t["id"], "running")
    with pytest.raises(TaskError, match="running"):
        tasks.move(conn, t["id"], "hold")
    with pytest.raises(TaskError, match="running"):
        tasks.update(conn, t["id"], {"title": "b"})
    with pytest.raises(TaskError, match="running"):
        tasks.delete(conn, t["id"])


def test_update(conn, repo, plain_dir):
    t = tasks.create(conn, "a", plain_dir)
    t = tasks.update(conn, t["id"], {"title": "New", "priority": "low", "model": "",
                                     "max_windows": "auto", "threshold": "40",
                                     "project_dir": str(repo)})
    assert t["title"] == "New"
    assert t["priority"] == 3
    assert t["model"] is None
    assert t["max_windows"] == "auto"
    assert t["threshold"] == 40.0
    assert t["repo_root"] == str(repo)
    assert t["base_branch"] == "main"
    with pytest.raises(TaskError, match="cannot change"):
        tasks.update(conn, t["id"], {"status": "done"})


def test_project_is_fixed_after_the_first_attempt(conn, repo, plain_dir):
    t = tasks.create(conn, "a", repo)
    tasks.set_status(conn, t["id"], "queued", attempts=1)
    with pytest.raises(TaskError, match="fixed"):
        tasks.update(conn, t["id"], {"project_dir": str(plain_dir)})
    # Sending the same value back, as the edit form does, is fine.
    tasks.update(conn, t["id"], {"project_dir": str(repo), "title": "b"})


def test_task_file_round_trip(conn, repo):
    text = f"""---
title: Migrate the client
project: {repo}
priority: high
effort: high
max-windows: 3
---
Migrate every call.

Then run the tests.
"""
    kwargs = tasks.parse_task_file(text)
    task = tasks.create(conn, **kwargs)
    assert task["title"] == "Migrate the client"
    assert task["priority"] == 1
    assert task["effort"] == "high"
    assert task["max_windows"] == "3"
    assert task["prompt"] == "Migrate every call.\n\nThen run the tests."
    again = tasks.parse_task_file(tasks.format_task_file(task))
    assert again["title"] == "Migrate the client"
    assert again["prompt"] == task["prompt"]


def test_task_file_without_front_matter_uses_the_default_project(plain_dir):
    kwargs = tasks.parse_task_file("Just a prompt\n", default_project=str(plain_dir))
    assert kwargs == {"prompt": "Just a prompt", "project_dir": str(plain_dir)}


def test_task_file_errors():
    with pytest.raises(TaskError, match="closing"):
        tasks.parse_task_file("---\ntitle: x\n")
    with pytest.raises(TaskError, match="unknown field"):
        tasks.parse_task_file("---\ncolour: red\n---\nx")


def test_settings_defaults_and_validation(conn):
    conf = settings.get_all(conn)
    assert conf["threshold"] == 50.0
    assert conf["away_minutes"] == 15
    assert conf["on_return"] == "stop"
    conf = settings.update(conn, {"threshold": 60, "away_enabled": False})
    assert conf["threshold"] == 60.0
    assert conf["away_enabled"] is False
    for bad in ({"threshold": 0}, {"threshold": "x"}, {"on_return": "maybe"},
                {"nope": 1}, {"max_windows": "many"}, {"away_enabled": "yes"}):
        with pytest.raises(ValueError):
            settings.update(conn, bad)
