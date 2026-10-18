import io

from claude_backlog import cli, tasks


def test_add_a_prompt(conn, plain_dir, capsys):
    assert cli.main(["add", "-C", str(plain_dir), "-p", "high", "Fix the tests"]) == 0
    out = capsys.readouterr().out
    assert "Added fix-the-tests-1" in out
    assert "not a git repository" in out
    task = tasks.get(conn, "fix-the-tests-1")
    assert task["priority"] == 1


def test_add_a_task_file_from_its_repo(conn, repo, capsys):
    path = repo / "task.md"
    path.write_text("---\ntitle: From a file\npriority: low\n---\nDo the work.\n")
    assert cli.main(["add", str(path)]) == 0
    task = tasks.queue(conn)[0]
    assert task["title"] == "From a file"
    assert task["project_dir"] == str(repo)
    assert task["priority"] == 3


def test_add_notes_uncommitted_changes(conn, repo, capsys):
    assert cli.main(["add", "-C", str(repo), "Clean task"]) == 0
    assert capsys.readouterr().err == ""
    (repo / "README.md").write_text("changed\n")
    assert cli.main(["add", "-C", str(repo), "Dirty task"]) == 0
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1
    assert f"{repo} has uncommitted changes" in captured.err
    task_id = captured.out.split()[1].rstrip(":")
    assert tasks.get(conn, task_id)["title"] == "Dirty task"
    assert tasks.get(conn, task_id)["status"] == "queued"


def test_add_from_stdin(conn, plain_dir, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("Prompt from stdin\nmore\n"))
    assert cli.main(["add", "-C", str(plain_dir), "--hold", "-t", "Stdin task", "-"]) == 0
    task = tasks.list_tasks(conn)[0]
    assert task["status"] == "hold"
    assert task["title"] == "Stdin task"
    assert task["prompt"] == "Prompt from stdin\nmore"


def test_add_error_is_reported(capsys):
    assert cli.main(["add", "-C", "/no/such", "x"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_list_and_export(conn, plain_dir, capsys):
    cli.main(["add", "-C", str(plain_dir), "First"])
    capsys.readouterr()
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "Queue:" in out and "first-1" in out
    cli.main(["export", "first-1"])
    out = capsys.readouterr().out
    assert out.startswith("---\ntitle: First\n")


def test_go_without_the_service(conn, monkeypatch):
    import pytest

    from claude_backlog import settings

    settings.update(conn, {"port": 1})  # nothing listens there
    with pytest.raises(SystemExit, match="not running"):
        cli.main(["go"])


def test_unit_file_runs_this_python_with_path():
    from claude_backlog.install import desktop_text, unit_text

    unit = unit_text(python="/opt/py/bin/python", path_env="/usr/bin:/bin")
    assert "ExecStart=/opt/py/bin/python -m claude_backlog serve" in unit
    assert "Environment=PATH=/usr/bin:/bin" in unit
    assert "KillMode=mixed" in unit
    assert "WantedBy=default.target" in unit
    assert "http://127.0.0.1:8765/" in desktop_text(8765)


def test_report_quotes_summaries(conn, plain_dir, backlog_home):
    from claude_backlog import report
    from claude_backlog.runner import Attempt

    task = tasks.create(conn, "Write docs", plain_dir)
    folder = backlog_home / "tasks" / task["id"]
    folder.mkdir(parents=True)
    (folder / "SUMMARY.md").write_text("# Summary\nAll done.\n")
    attempt = Attempt(task["id"], task["title"], 0, "done", "done", True, False,
                      started_at=0.0, ended_at=3700.0, commits=2)
    path = report.write(conn, 0.0, 3700.0, "the queue is empty", "away", [attempt])
    text = path.read_text()
    assert "Started because: you were away. Stopped: the queue is empty." in text
    assert "| done | 0 | 1h01m | 2 |" in text
    assert "> # Summary" in text
    assert report.summary_line("the queue is empty", [attempt]).startswith(
        "Stopped: the queue is empty. 1 done")


def test_phone_setup_shows_the_enable_link_when_serve_hangs(monkeypatch, capsys):
    import subprocess

    from claude_backlog import install

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"],
                                        output=b"Serve is not enabled.\nhttps://enable\n")

    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(install.subprocess, "run", hang)
    assert install.setup_phone(8765) == 1
    out = capsys.readouterr().out
    assert "https://enable" in out
    assert "install --phone' again" in out


def test_template_command(conn, capsys):
    from claude_backlog import settings

    assert cli.main(["template"]) == 0
    assert capsys.readouterr().out.startswith("## Goal\n<")
    settings.update(conn, {"prompt_template": ""})
    cli.main(["template"])
    assert "No prompt template" in capsys.readouterr().out
