"""Tasks on the same project: "start after" and the merge check."""

import sqlite3

import pytest

from claude_backlog import cli, db, gitops, paths, tasks
from claude_backlog.runner import Runner
from claude_backlog.tasks import TaskError
from conftest import git
from test_runner import FakeInhibitor, run_queue
from test_server import client  # noqa: F401  (the fixture)


def make_runner():
    return Runner(inhibitor=FakeInhibitor(), notifier=lambda t, b: None)


def done_in(conn, task_id):
    return tasks.get(conn, task_id)["status"] == "done"


# --- start after ----------------------------------------------------------------


def test_start_after_is_checked(conn, repo, plain_dir, tmp_path):
    a = tasks.create(conn, "a", repo)
    b = tasks.create(conn, "b", repo, after_task=a["id"])
    assert b["after_task"] == a["id"]
    with pytest.raises(TaskError, match="no task"):
        tasks.create(conn, "c", repo, after_task="nope-99")
    with pytest.raises(TaskError, match="same repository"):
        tasks.create(conn, "c", plain_dir, after_task=a["id"])
    with pytest.raises(TaskError, match="itself"):
        tasks.update(conn, a["id"], {"after_task": a["id"]})
    with pytest.raises(TaskError, match="each other"):
        tasks.update(conn, a["id"], {"after_task": b["id"]})
    # Clearing it from the edit form sends an empty value.
    assert tasks.update(conn, b["id"], {"after_task": ""})["after_task"] is None
    # Two folders that are not repositories only match when they are the same.
    other = tmp_path / "other"
    other.mkdir()
    p = tasks.create(conn, "p", plain_dir)
    tasks.create(conn, "q", plain_dir, after_task=p["id"])
    with pytest.raises(TaskError, match="same repository"):
        tasks.create(conn, "r", other, after_task=p["id"])


def test_start_after_is_fixed_once_the_task_has_run(conn, repo):
    a = tasks.create(conn, "a", repo)
    b = tasks.create(conn, "b", repo)
    tasks.set_status(conn, b["id"], "queued", attempts=1)
    with pytest.raises(TaskError, match="fixed"):
        tasks.update(conn, b["id"], {"after_task": a["id"]})
    tasks.update(conn, b["id"], {"after_task": "", "title": "same"})


def test_a_waiting_task_is_skipped_until_the_other_is_done(conn, repo):
    a = tasks.create(conn, "a", repo, status="hold")
    b = tasks.create(conn, "b", repo, after_task=a["id"])
    assert tasks.waits_for(conn, b)["id"] == a["id"]
    assert tasks.runnable(conn) == []
    assert tasks.next_queued(conn) is None
    tasks.set_status(conn, a["id"], "done")
    assert tasks.waits_for(conn, tasks.get(conn, b["id"])) is None
    assert tasks.next_queued(conn)["id"] == b["id"]
    tasks.set_status(conn, a["id"], "archived")
    assert tasks.next_queued(conn)["id"] == b["id"]


def test_deleting_a_task_frees_the_ones_after_it(conn, repo):
    a = tasks.create(conn, "a", repo, status="hold")
    b = tasks.create(conn, "b", repo, after_task=a["id"])
    tasks.delete(conn, a["id"])
    assert tasks.get(conn, b["id"])["after_task"] is None
    assert tasks.next_queued(conn)["id"] == b["id"]


def test_task_file_keeps_start_after(conn, repo):
    a = tasks.create(conn, "a", repo)
    b = tasks.create(conn, "b", repo, after_task=a["id"])
    text = tasks.format_task_file(b)
    assert f"after: {a['id']}" in text
    assert tasks.parse_task_file(text)["after_task"] == a["id"]


def test_cli_add_after(conn, repo, capsys):
    a = tasks.create(conn, "a", repo)
    assert cli.main(["add", "-C", str(repo), "--after", a["id"], "Then this"]) == 0
    assert f"starts after: {a['id']}" in capsys.readouterr().out
    assert tasks.queue(conn)[-1]["after_task"] == a["id"]


def test_the_runner_starts_it_from_the_other_tasks_branch(conn, repo, plan, calls):
    # Queued first, but it waits; the other task runs, then this one, in one run.
    b = tasks.create(conn, "b", repo)
    a = tasks.create(conn, "a", repo)
    tasks.update(conn, b["id"], {"after_task": a["id"]})
    plan({"exit": 0, "summary": True, "commit": True, "file": "a.txt"},
         {"exit": 0, "summary": True, "commit": True, "file": "b.txt"})
    run_queue(make_runner())
    assert done_in(conn, a["id"]) and done_in(conn, b["id"])
    assert len(calls()) == 2
    b = tasks.get(conn, b["id"])
    assert b["base_commit"] == git(repo, "rev-parse", f"backlog/{a['id']}")
    assert b["base_ref"] == f"backlog/{a['id']}"
    prompt = (paths.task_dir(b["id"]) / "prompt.md").read_text()
    assert f"started\n  from backlog/{a['id']} at" in prompt
    assert 'the branch of the task "a"' in prompt
    files = git(repo, "ls-tree", "--name-only", f"backlog/{b['id']}").split()
    assert "a.txt" in files and "b.txt" in files
    # Its own commits are only its own.
    assert len(gitops.commits(str(repo), b["base_commit"], b["branch"])) == 1


def test_after_a_merged_task_it_starts_from_the_base_branch(conn, repo, plan):
    a = tasks.create(conn, "a", repo)
    plan({"exit": 0, "summary": True, "commit": True, "file": "a.txt"})
    run_queue(make_runner())
    git(repo, "merge", "-q", "--no-edit", f"backlog/{a['id']}")
    (repo / "later.txt").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "later on main")
    b = tasks.create(conn, "b", repo, after_task=a["id"])
    plan({"exit": 0, "summary": True})
    run_queue(make_runner())
    b = tasks.get(conn, b["id"])
    assert b["base_commit"] == git(repo, "rev-parse", "main")
    assert b["base_ref"] is None
    assert "from main at" in (paths.task_dir(b["id"]) / "prompt.md").read_text()


def test_the_run_says_why_it_stopped_when_only_waiting_tasks_are_left(conn, repo):
    a = tasks.create(conn, "a", repo, status="hold")
    tasks.create(conn, "b", repo, after_task=a["id"])
    r = make_runner()
    run_queue(r)
    assert r.last_stop["reason"] == "the tasks left in the queue wait for other tasks"


def test_old_databases_get_the_new_column(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.MIGRATIONS[1])
    old.close()
    conn = db.connect(path)
    version = conn.execute("SELECT schema_version FROM meta").fetchone()[0]
    assert version == db.SCHEMA_VERSION
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert {"after_task", "base_ref"} <= columns
    conn.close()


# --- the merge check ------------------------------------------------------------


def branch_with(repo, name, files, base="main"):
    git(repo, "branch", name, base)
    git(repo, "switch", "-q", name)
    for file, text in files.items():
        (repo / file).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", f"work on {name}")
    git(repo, "switch", "-q", "main")


def test_merge_conflicts(repo):
    branch_with(repo, "one", {"README.md": "one\n"})
    branch_with(repo, "two", {"README.md": "two\n"})
    branch_with(repo, "three", {"other.txt": "x\n"})
    assert gitops.merge_conflicts(str(repo), "one", "two") == ["README.md"]
    assert gitops.merge_conflicts(str(repo), "one", "three") == []
    assert gitops.merge_conflicts(str(repo), "one", "no-such-branch") is None
    assert gitops.is_ancestor(str(repo), "main", "one")
    assert not gitops.is_ancestor(str(repo), "one", "two")
    assert set(gitops.branch_tips(str(repo))) == {"main", "one", "two", "three"}


def finished(conn, repo, title, files):
    task = tasks.create(conn, title, repo)
    base = git(repo, "rev-parse", "main")
    branch = f"backlog/{task['id']}"
    branch_with(repo, branch, files)
    return tasks.set_status(conn, task["id"], "done", branch=branch, base_commit=base,
                            attempts=1)


def cards_by_title(client):  # noqa: F811
    return {c["title"]: c for c in client.get("/api/state").json()["tasks"]}


def test_the_board_shows_conflicts_between_finished_tasks(client, conn, repo):  # noqa: F811
    finished(conn, repo, "one", {"README.md": "one\n"})
    finished(conn, repo, "two", {"README.md": "two\n"})
    finished(conn, repo, "three", {"other.txt": "x\n"})
    cards = cards_by_title(client)
    assert [c["with"] for c in cards["one"]["conflicts"]] == ["two"]
    assert cards["one"]["conflicts"][0]["files"] == ["README.md"]
    assert [c["with"] for c in cards["two"]["conflicts"]] == ["one"]
    assert cards["three"]["conflicts"] == []


def test_the_board_shows_conflicts_with_the_base_branch_and_merges(client, conn, repo):  # noqa: F811
    one = finished(conn, repo, "one", {"README.md": "one\n"})
    two = finished(conn, repo, "two", {"other.txt": "x\n"})
    (repo / "README.md").write_text("changed on main\n")
    git(repo, "commit", "-q", "-am", "main moves on")
    git(repo, "merge", "-q", "--no-edit", two["branch"])
    cards = cards_by_title(client)
    assert cards["one"]["conflicts"] == [{"with": "main", "task_id": None,
                                          "files": ["README.md"]}]
    assert cards["two"]["merged"] is True
    assert cards["two"]["conflicts"] == []
    detail = client.get(f"/api/tasks/{one['id']}").json()
    assert detail["task"]["conflicts"][0]["files"] == ["README.md"]


def test_the_board_shows_who_a_task_waits_for(client, conn, repo):  # noqa: F811
    a = tasks.create(conn, "first", repo, status="hold")
    r = client.post("/api/tasks", json={"prompt": "second", "project_dir": str(repo),
                                        "after_task": a["id"]})
    assert r.status_code == 200
    card = cards_by_title(client)["second"]
    assert card["after_title"] == "first"
    assert card["waiting"] is True
    tasks.set_status(conn, a["id"], "done")
    assert cards_by_title(client)["second"]["waiting"] is False
    info = client.get("/api/project-info", params={"path": str(repo)}).json()
    assert info["root"] == str(repo)

