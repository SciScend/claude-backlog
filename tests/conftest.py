import json
import subprocess
import sys
from pathlib import Path

import pytest

from claude_backlog import db

FAKE = Path(__file__).parent / "fake_nightshift.py"


@pytest.fixture(autouse=True)
def backlog_home(tmp_path, monkeypatch):
    """Every test gets its own data folder and never runs the real nightshift."""
    home = tmp_path / "backlog-home"
    monkeypatch.setenv("CLAUDE_BACKLOG_HOME", str(home))
    monkeypatch.setenv("CLAUDE_BACKLOG_NIGHTSHIFT", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("FAKE_NS_CALLS", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("FAKE_NS_PLAN", str(tmp_path / "plan.json"))
    (tmp_path / "plan.json").write_text("[]")
    return home


@pytest.fixture
def conn(backlog_home):
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def plan(tmp_path):
    """Set what the fake nightshift does on each call."""
    def set_plan(*steps):
        (tmp_path / "plan.json").write_text(json.dumps(list(steps)))
    return set_plan


@pytest.fixture
def task_plans(tmp_path):
    """Set what the fake nightshift does, per task id: {task_id: [step, ...]}."""
    def set_plans(steps_by_task):
        (tmp_path / "plan.json").write_text(json.dumps(steps_by_task))
    return set_plans


@pytest.fixture
def calls(tmp_path):
    def read():
        path = tmp_path / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]
    return read


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("hello\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "first")
    return path


@pytest.fixture
def plain_dir(tmp_path):
    path = tmp_path / "plain"
    path.mkdir()
    return path
