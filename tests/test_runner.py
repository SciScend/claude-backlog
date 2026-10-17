import time

import pytest

from claude_backlog import paths, runner, settings, tasks
from claude_backlog.runner import Runner, map_exit
from conftest import git


class FakeInhibitor:
    available = True

    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self, why):
        self.started += 1
        return True

    def stop(self):
        self.stopped += 1


@pytest.fixture
def make_runner():
    notes = []

    def make():
        r = Runner(inhibitor=FakeInhibitor(), notifier=lambda t, b: notes.append(b))
        r.notes = notes
        return r
    return make


def run_queue(r, trigger="run-now", until=None):
    assert r.start(trigger, until)
    r.wait(30)
    assert r.state == "idle"


@pytest.mark.parametrize("code, summary, attempts, expected", [
    (0, True, 1, ("done", "done", True, False)),
    (0, False, 1, ("attention", "attention", True, False)),
    (1, False, 1, ("attention", "attention", True, False)),
    (2, False, 1, ("attention", "attention", True, False)),
    (3, False, 1, ("limit", "queued", False, True)),
    (4, False, 1, ("deferred", "queued", True, False)),
    (4, False, 3, ("attention", "attention", True, False)),
    (5, False, 1, ("usage-unknown", "queued", False, True)),
    (130, False, 1, ("interrupted", "queued", False, True)),
    (-9, False, 1, ("attention", "attention", True, False)),
])
def test_map_exit(code, summary, attempts, expected):
    m = map_exit(code, summary, attempts, 3)
    assert (m.result, m.status, m.counted, m.stop_queue) == expected


def test_a_task_runs_on_its_own_branch(conn, repo, plan, calls, make_runner):
    task = tasks.create(conn, "Add work", repo)
    plan({"exit": 0, "summary": True, "commit": True})
    r = make_runner()
    run_queue(r)

    done = tasks.get(conn, task["id"])
    assert done["status"] == "done"
    assert done["attempts"] == 1
    assert done["branch"] == f"backlog/{task['id']}"
    assert done["finished_at"]
    # The work is on the branch, not in my working copy.
    assert not (repo / "work.txt").exists()
    assert git(repo, "log", "--format=%s", "main..backlog/" + task["id"]) == "fake work"
    assert git(repo, "branch", "--show-current") == "main"

    call = calls()[0]
    assert call["cwd"] == str(paths.worktree_dir(task["id"]))
    args = call["args"]
    assert args[args.index("-t") + 1] == "50"
    assert args[args.index("-w") + 1] == "2"
    assert args[args.index("--status-file") + 1].endswith("nightshift-status.md")
    prompt = (paths.task_dir(task["id"]) / "prompt.md").read_text()
    assert "on the branch backlog/" in prompt
    assert r.inhibitor.started == 1 and r.inhibitor.stopped == 1
    assert r.last_stop["reason"] == "the queue is empty"
    assert "1 done" in r.notes[0]
    assert list(paths.reports_dir().glob("*.md"))


def test_leftover_changes_are_committed(conn, repo, plan, make_runner):
    task = tasks.create(conn, "x", repo)
    plan({"exit": 4, "dirty": True}, {"exit": 3})
    run_queue(make_runner())
    subjects = git(repo, "log", "--format=%s", "main..backlog/" + task["id"])
    assert "left uncommitted" in subjects
    t = tasks.get(conn, task["id"])
    assert t["status"] == "queued"
    assert t["attempts"] == 1


def test_a_later_attempt_reuses_the_branch(conn, repo, plan, make_runner):
    task = tasks.create(conn, "x", repo)
    plan({"exit": 4, "commit": True}, {"exit": 0, "summary": True, "commit": True})
    run_queue(make_runner())
    t = tasks.get(conn, task["id"])
    assert t["status"] == "done"
    assert t["attempts"] == 2
    log = git(repo, "log", "--format=%s", "main..backlog/" + task["id"]).splitlines()
    assert log == ["fake work", "fake work"]


def test_too_many_attempts_need_attention(conn, plain_dir, plan, make_runner):
    settings.update(conn, {"max_attempts": 2})
    task = tasks.create(conn, "x", plain_dir)
    plan({"exit": 4}, {"exit": 4})
    run_queue(make_runner())
    t = tasks.get(conn, task["id"])
    assert t["status"] == "attention"
    assert "2 attempt" in t["note"]


def test_weekly_limit_stops_the_queue(conn, plain_dir, plan, calls, make_runner):
    first = tasks.create(conn, "first", plain_dir)
    second = tasks.create(conn, "second", plain_dir)
    plan({"exit": 3})
    r = make_runner()
    run_queue(r)
    assert len(calls()) == 1
    assert tasks.get(conn, first["id"])["status"] == "queued"
    assert tasks.get(conn, first["id"])["attempts"] == 0
    assert tasks.get(conn, second["id"])["status"] == "queued"
    assert r.last_stop["reason"] == "weekly limit reached"


def test_usage_unknown_stops_the_queue(conn, plain_dir, plan, calls, make_runner):
    tasks.create(conn, "first", plain_dir)
    tasks.create(conn, "second", plain_dir)
    plan({"exit": 5})
    r = make_runner()
    run_queue(r)
    assert len(calls()) == 1
    assert r.last_stop["reason"] == "weekly usage could not be read"


def test_each_task_gets_a_turn_before_a_second_one(conn, plain_dir, plan, calls,
                                                    make_runner):
    big = tasks.create(conn, "big", plain_dir, priority="high")
    small = tasks.create(conn, "small", plain_dir)
    plan({"exit": 4}, {"exit": 0, "summary": True}, {"exit": 0, "summary": True})
    run_queue(make_runner())
    order = [c["args"][c["args"].index("-f") + 1].split("/")[-2] for c in calls()]
    assert order == [big["id"], small["id"], big["id"]]


def test_failures_move_on_to_the_next_task(conn, plain_dir, plan, make_runner):
    bad = tasks.create(conn, "bad", plain_dir)
    good = tasks.create(conn, "good", plain_dir)
    plan({"exit": 1}, {"exit": 0, "summary": True})
    run_queue(make_runner())
    assert tasks.get(conn, bad["id"])["status"] == "attention"
    assert tasks.get(conn, good["id"])["status"] == "done"


def test_exit_0_without_summary_needs_attention(conn, plain_dir, plan, make_runner):
    t = tasks.create(conn, "x", plain_dir)
    plan({"exit": 0})
    run_queue(make_runner())
    t = tasks.get(conn, t["id"])
    assert t["status"] == "attention"
    assert "SUMMARY.md" in t["note"]


def test_non_git_task_runs_in_place(conn, plain_dir, plan, calls, make_runner):
    t = tasks.create(conn, "x", plain_dir)
    plan({"exit": 0, "summary": True})
    run_queue(make_runner())
    assert calls()[0]["cwd"] == str(plain_dir)
    assert tasks.get(conn, t["id"])["branch"] is None


def test_running_again_keeps_the_old_summary_aside(conn, plain_dir, plan, make_runner):
    t = tasks.create(conn, "x", plain_dir)
    plan({"exit": 0, "summary": True}, {"exit": 0})
    run_queue(make_runner())
    tasks.move(conn, t["id"], "queued")
    run_queue(make_runner())
    assert tasks.get(conn, t["id"])["status"] == "attention"
    assert list(paths.task_dir(t["id"]).glob("SUMMARY-*.md"))


def test_stop_now_puts_the_task_back(conn, plain_dir, plan, make_runner):
    t = tasks.create(conn, "x", plain_dir)
    tasks.create(conn, "y", plain_dir)
    plan({"sleep": True})
    r = make_runner()
    assert r.start("run-now")
    deadline = time.time() + 10
    while r.proc is None and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    assert r.stop(now=True)
    r.wait(20)
    assert r.state == "idle"
    task = tasks.get(conn, t["id"])
    assert task["status"] == "queued"
    assert task["attempts"] == 0
    assert r.last_stop["reason"] == "stopped by you"


def test_stop_after_the_task(conn, plain_dir, plan, calls, make_runner):
    tasks.create(conn, "x", plain_dir)
    tasks.create(conn, "y", plain_dir)
    plan({"sleep": True})
    r = make_runner()
    r.start("run-now")
    deadline = time.time() + 10
    while r.proc is None and time.time() < deadline:
        time.sleep(0.05)
    r.stop(now=False)
    r.wait(30)
    assert len(calls()) == 1


def test_until_in_the_past_starts_nothing(conn, plain_dir, calls, make_runner):
    tasks.create(conn, "x", plain_dir)
    r = make_runner()
    run_queue(r, until=time.time() - 1)
    assert calls() == []
    assert "until" in r.last_stop["reason"]


def test_run_now_takes_over_an_away_run(make_runner, conn, plain_dir, plan):
    tasks.create(conn, "x", plain_dir)
    plan({"sleep": True})
    r = make_runner()
    r.start("away")
    assert not r.start("away")
    assert r.start("run-now")
    assert r.trigger == "run-now"
    r.stop(now=True)
    r.wait(20)


def test_recover_puts_running_tasks_back(conn, plain_dir):
    t = tasks.create(conn, "x", plain_dir)
    tasks.set_status(conn, t["id"], "running")
    conn.execute("INSERT INTO runs (task_id, started_at) VALUES (?, 1)", (t["id"],))
    assert runner.recover(conn) == [t["id"]]
    task = tasks.get(conn, t["id"])
    assert task["status"] == "queued"
    assert "service stopped" in task["note"]
    row = conn.execute("SELECT result, ended_at FROM runs").fetchone()
    assert row["result"] == "interrupted" and row["ended_at"]


def test_a_worktree_that_cannot_be_made_needs_attention(conn, repo, make_runner):
    t = tasks.create(conn, "x", repo)
    tasks.update(conn, t["id"], {"base_branch": "no-such-branch"})
    run_queue(make_runner())
    task = tasks.get(conn, t["id"])
    assert task["status"] == "attention"
    assert "worktree" in task["note"]
