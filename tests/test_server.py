import pytest
from starlette.testclient import TestClient

from claude_backlog import settings, tasks
from claude_backlog.server import MAX_CHANGED_FILES, Service, create_app
from conftest import git

LOCAL = "http://127.0.0.1:8765"


class IdleRunner:
    version = 0

    class inhibitor:
        available = True

    def __init__(self):
        self.started = []
        self.stopped = []
        self.state = "idle"

    def status(self):
        return {"state": self.state, "trigger": None, "until": None, "started_at": None,
                "task_id": None, "task_started_at": None, "stop_mode": None,
                "last_stop": None, "inhibit": True}

    def start(self, trigger, until=None):
        self.started.append((trigger, until))
        return True

    def stop(self, now=False, cause="you"):
        self.stopped.append(now)
        return False


class NoUsage:
    read_at = 0

    def get(self, force=False):
        return {"usage": {"weekly_pct": 20.0}, "error": None, "read_at": 0}


@pytest.fixture
def client(backlog_home):
    service = Service(runner_=IdleRunner(), usage=NoUsage(), port=8765)
    app = create_app(service)
    with TestClient(app, base_url=LOCAL) as c:
        c.service = service
        yield c
    service.close()


def post(client, path, body=None, **headers):
    return client.post(path, json=body or {}, headers=headers)


def test_state(client):
    data = client.get("/api/state").json()
    assert data["tasks"] == []
    assert data["settings"]["threshold"] == 50.0
    assert data["settings"]["prompt_template"].startswith("## Goal\n<")
    assert data["default_prompt_template"] == settings.PROMPT_TEMPLATE
    assert data["usage"]["usage"]["weekly_pct"] == 20.0


def test_index_and_static(client):
    assert "Claude Backlog" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_create_move_and_act(client, plain_dir):
    r = post(client, "/api/tasks", {"prompt": "Do it", "project_dir": str(plain_dir)})
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]["id"]
    r = post(client, "/api/tasks", {"prompt": "Other", "project_dir": str(plain_dir)})
    other = r.json()["task"]["id"]
    r = post(client, f"/api/tasks/{other}/action", {"action": "run-next"})
    assert r.status_code == 200
    queue = [t["id"] for t in client.get("/api/state").json()["tasks"]]
    assert queue[0] == other
    r = post(client, f"/api/tasks/{task_id}/move", {"status": "hold"})
    assert r.json()["task"]["status"] == "hold"
    r = client.patch(f"/api/tasks/{task_id}", json={"title": "Renamed"})
    assert r.json()["task"]["title"] == "Renamed"
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["task"]["title"] == "Renamed"
    assert detail["runs"] == []
    r = post(client, f"/api/tasks/{task_id}/action", {"action": "delete"})
    assert r.status_code == 200
    assert client.get(f"/api/tasks/{task_id}").status_code == 400


def test_errors_are_json(client):
    r = post(client, "/api/tasks", {"prompt": "x", "project_dir": "/no/such"})
    assert r.status_code == 400
    assert "does not exist" in r.json()["error"]
    r = client.put("/api/settings", json={"threshold": 500})
    assert r.status_code == 400


def test_summary_is_rendered_without_html(client, plain_dir, backlog_home):
    task = post(client, "/api/tasks", {"prompt": "x", "project_dir": str(plain_dir)}).json()
    task_id = task["task"]["id"]
    folder = backlog_home / "tasks" / task_id
    folder.mkdir(parents=True)
    (folder / "SUMMARY.md").write_text("# Done\n\n<script>alert(1)</script>\n\n"
                                       "## Decisions for me\n\n- a\n")
    conn = client.service.conn()
    tasks.set_status(conn, task_id, "done", attempts=1, finished_at=5.0)
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert "<h1>Done</h1>" in detail["summary_html"]
    assert "<script>" not in detail["summary_html"]
    card = next(t for t in client.get("/api/state").json()["tasks"] if t["id"] == task_id)
    assert card["decisions"] == 1
    assert card["new"]


def test_run_and_stop(client):
    assert post(client, "/api/run", {"until": "07:30"}).status_code == 200
    trigger, until = client.service.runner.started[0]
    assert trigger == "run-now" and until
    assert post(client, "/api/run", {"until": "7.30"}).status_code == 400
    post(client, "/api/stop", {"now": True})
    assert client.service.runner.stopped == [True]


def test_settings(client):
    r = client.put("/api/settings", json={"threshold": 65, "away_minutes": 10})
    assert r.json()["settings"]["threshold"] == 65.0


def test_guard_refuses_other_hosts(client):
    r = client.get("/api/state", headers={"Host": "evil.example"})
    assert r.status_code == 403


def test_guard_refuses_cross_site_writes(client):
    r = post(client, "/api/run", {}, Origin="http://evil.example")
    assert r.status_code == 403
    r = client.post("/api/run", content=b"until=", headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403
    r = post(client, "/api/run", {}, Origin=LOCAL)
    assert r.status_code == 200


def test_phone_port_needs_the_right_tailscale_login(backlog_home):
    # The phone port is the board port + 1; make it 443 to match the test URL.
    service = Service(runner_=IdleRunner(), usage=NoUsage(), port=442)
    app = create_app(service)
    with TestClient(app, base_url="https://box.tailnet.ts.net") as c:
        assert c.get("/api/state").status_code == 403  # no login configured
        settings.update(service.conn(), {"tailscale_login": "me@example.com"})
        assert c.get("/api/state").status_code == 403  # no header
        r = c.get("/api/state", headers={"Tailscale-User-Login": "someone@else.com"})
        assert r.status_code == 403
        r = c.get("/api/state", headers={"Tailscale-User-Login": "me@example.com"})
        assert r.status_code == 200
        r = c.post("/api/run", json={}, headers={"Tailscale-User-Login": "me@example.com",
                                                 "Origin": "https://box.tailnet.ts.net"})
        assert r.status_code == 200
        r = c.post("/api/run", json={}, headers={"Tailscale-User-Login": "me@example.com",
                                                 "Origin": "https://evil.example"})
        assert r.status_code == 403
    service.close()


def test_project_info(client, repo, plain_dir):
    info = client.get("/api/project-info", params={"path": str(repo)}).json()
    assert info == {"exists": True, "git": True, "root": str(repo), "branch": "main",
                    "branches": ["main"], "dirty": False}
    info = client.get("/api/project-info", params={"path": str(plain_dir)}).json()
    assert info["git"] is False
    assert client.get("/api/project-info", params={"path": "/no/such"}).json() == {
        "exists": False}


def test_project_changes(client, repo):
    changes = client.get("/api/project-changes", params={"path": str(repo)}).json()
    assert changes == {"root": str(repo), "branch": "main", "files": [], "total": 0}
    (repo / "README.md").write_text("changed\n")
    (repo / "new file.txt").write_text("new\n")
    changes = client.get("/api/project-changes", params={"path": str(repo)}).json()
    assert changes["files"] == [{"status": " M", "path": "README.md"},
                                {"status": "??", "path": "new file.txt"}]
    assert changes["total"] == 2
    for i in range(MAX_CHANGED_FILES + 5):
        (repo / f"extra-{i:02}.txt").write_text("x\n")
    changes = client.get("/api/project-changes", params={"path": str(repo)}).json()
    assert len(changes["files"]) == MAX_CHANGED_FILES
    assert changes["total"] == MAX_CHANGED_FILES + 7


def test_project_changes_needs_a_repository(client, plain_dir):
    r = client.get("/api/project-changes", params={"path": str(plain_dir)})
    assert r.status_code == 400
    assert "not a git repository" in r.json()["error"]


def test_commit_now(client, repo):
    (repo / ".gitignore").write_text("secret.txt\n")
    (repo / "secret.txt").write_text("do not commit\n")
    (repo / "README.md").write_text("changed\n")
    (repo / "notes.md").write_text("new\n")
    r = post(client, "/api/project-commit",
             {"path": str(repo), "message": "Save my work before the task"})
    assert r.status_code == 200, r.text
    assert r.json()["branch"] == "main"
    assert r.json()["commit"] == git(repo, "rev-parse", "--short", "HEAD")
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "log", "-1", "--format=%s") == "Save my work before the task"
    assert git(repo, "ls-files") == ".gitignore\nREADME.md\nnotes.md"
    assert (repo / "secret.txt").exists()
    info = client.get("/api/project-info", params={"path": str(repo)}).json()
    assert info["dirty"] is False


def test_commit_now_refuses(client, repo, plain_dir):
    (repo / "README.md").write_text("changed\n")
    for message in ("", "   "):
        r = post(client, "/api/project-commit", {"path": str(repo), "message": message})
        assert r.status_code == 400
        assert "commit message" in r.json()["error"]
    r = post(client, "/api/project-commit", {"path": str(plain_dir), "message": "Save"})
    assert r.status_code == 400
    assert "not a git repository" in r.json()["error"]
    assert git(repo, "log", "-1", "--format=%s") == "first"
    git(repo, "checkout", "-q", "--", "README.md")
    r = post(client, "/api/project-commit", {"path": str(repo), "message": "Save"})
    assert r.status_code == 409
    assert "nothing to commit" in r.json()["error"]


def test_commit_now_refuses_a_detached_head(client, repo):
    git(repo, "checkout", "-q", "--detach")
    (repo / "README.md").write_text("changed\n")
    r = post(client, "/api/project-commit", {"path": str(repo), "message": "Save"})
    assert r.status_code == 409
    assert "detached HEAD" in r.json()["error"]
    assert git(repo, "status", "--porcelain") == "M README.md"


def test_commit_now_needs_a_git_identity(client, tmp_path, monkeypatch):
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                 "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "anonymous"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("a\n")
    git(path, "add", ".")
    git(path, "-c", "user.name=Setup", "-c", "user.email=setup@example.com",
        "commit", "-q", "-m", "first")
    (path / "a.txt").write_text("changed\n")
    r = post(client, "/api/project-commit", {"path": str(path), "message": "Save"})
    assert r.status_code == 409
    error = r.json()["error"]
    assert "does not know who you are" in error
    assert "git config --global user.email" in error
    assert git(path, "log", "-1", "--format=%s") == "first"
    assert git(path, "status", "--porcelain") == "M a.txt"


def test_commit_now_runs_the_hooks(client, repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint failed: README.md is too short' >&2\nexit 1\n")
    hook.chmod(0o755)
    (repo / "README.md").write_text("changed\n")
    (repo / "notes.md").write_text("new\n")
    r = post(client, "/api/project-commit", {"path": str(repo), "message": "Save"})
    assert r.status_code == 409
    error = r.json()["error"]
    assert "still uncommitted" in error
    assert "lint failed: README.md is too short" in error
    assert git(repo, "log", "-1", "--format=%s") == "first"
    # Nothing is left staged either: the index is as it was.
    assert git(repo, "status", "--porcelain") == "M README.md\n?? notes.md"


def test_projects_and_folders(client, tmp_path):
    root = tmp_path / "work"
    for name in ("app/.git", "lib", "group/site", "group/notes", "plain/sub", ".hidden/x"):
        (root / name).mkdir(parents=True)
    (root / "lib" / "pyproject.toml").write_text("")
    (root / "group" / "site" / "package.json").write_text("")
    client.put("/api/settings", json={"project_roots": [str(root)]})
    data = client.get("/api/projects").json()
    assert data["folders"] == [str(root / "app"), str(root / "group" / "site"),
                               str(root / "lib")]
    data = client.get("/api/folders", params={"path": str(root)}).json()
    assert data["folders"] == [
        {"path": str(root / "app"), "project": True},
        {"path": str(root / "group"), "project": False},
        {"path": str(root / "lib"), "project": True},
        {"path": str(root / "plain"), "project": False},
    ]
    assert client.get("/api/folders", params={"path": "/no/such"}).json()["folders"] == []


def test_projects_through_a_symlink_listed_once(client, tmp_path):
    import os

    real = tmp_path / "data" / "projects"
    for name in ("app", "other"):
        (real / name).mkdir(parents=True)
        (real / name / "pyproject.toml").write_text("")
    link = tmp_path / "projects"
    link.symlink_to(real, target_is_directory=True)
    real = real.resolve()

    def listed():
        data = client.get("/api/projects").json()
        return data["recent"] + data["folders"]

    def assert_once(paths):
        reals = [os.path.realpath(p) for p in paths]
        assert sorted(reals) == sorted(set(reals)), paths
        assert set(reals) == {str(real / "app"), str(real / "other")}

    client.put("/api/settings", json={"project_roots": [str(link)]})
    r = post(client, "/api/tasks", {"prompt": "Do it", "project_dir": str(link / "app")})
    assert r.status_code == 200, r.text
    data = client.get("/api/projects").json()
    assert data["recent"] == [str(real / "app")]
    assert data["folders"] == [str(link / "other")]
    assert_once(listed())
    # Two roots that are the same real folder count as one.
    roots = [str(link), str(real), str(tmp_path)]
    client.put("/api/settings", json={"project_roots": roots})
    assert_once(listed())


# --- the prompt drafted in the background ----------------------------------------


@pytest.fixture
def fake_claude(monkeypatch, tmp_path):
    """The fake claude, held back until release() is called."""
    import sys
    from pathlib import Path

    fake = Path(__file__).with_name("fake_claude.py")
    gate = tmp_path / "release-claude"
    monkeypatch.setenv("CLAUDE_BACKLOG_CLAUDE", f"{sys.executable} {fake}")
    monkeypatch.setenv("FAKE_CLAUDE_WAIT", str(gate))
    return lambda: gate.write_text("")


def drafted(client, task_id):
    client.service.wait_drafts()
    return client.get(f"/api/tasks/{task_id}").json()["task"]


def test_a_task_is_drafted_in_the_background(client, repo, fake_claude):
    r = post(client, "/api/tasks", {"description": "Make the imports fast\nwith a cache",
                                    "project_dir": str(repo), "priority": "high"})
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["status"] == "drafting"
    assert task["draft_target"] == "queued"
    assert task["title"] == "Make the imports fast"
    assert task["prompt"] == ""
    conn = client.service.conn()
    assert tasks.next_queued(conn) is None
    cards = client.get("/api/state").json()["tasks"]
    card = next(t for t in cards if t["id"] == task["id"])
    assert card["status"] == "drafting"
    # Nothing can start it or overwrite the draft meanwhile.
    r = post(client, f"/api/tasks/{task['id']}/action", {"action": "run-next"})
    assert r.status_code == 400 and "still writing" in r.json()["error"]
    r = post(client, f"/api/tasks/{task['id']}/move", {"status": "queued"})
    assert r.status_code == 400
    r = client.patch(f"/api/tasks/{task['id']}", json={"prompt": "mine"})
    assert r.status_code == 400
    fake_claude()
    task = drafted(client, task["id"])
    assert task["status"] == "queued"
    assert task["priority"] == 1
    assert task["prompt"] == f"## Goal\nDone in {repo}."
    assert task["title"] == "Speed up imports"
    assert task["questions"][0] == {"question": "Keep the old format?",
                                    "assumption": "Yes, it stays.", "blocking": False,
                                    "answer": ""}
    assert task["draft_error"] is None
    assert tasks.next_queued(conn)["id"] == task["id"]


def test_a_draft_goes_where_the_form_said(client, repo, fake_claude):
    fake_claude()
    r = post(client, "/api/tasks", {"description": "Tidy up", "project_dir": str(repo),
                                    "status": "hold"})
    assert drafted(client, r.json()["task"]["id"])["status"] == "hold"


def test_a_blocking_question_holds_the_task(client, repo, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "blocking")
    fake_claude()
    task_id = post(client, "/api/tasks", {"description": "Store the tasks",
                                          "project_dir": str(repo)}).json()["task"]["id"]
    task = drafted(client, task_id)
    assert task["status"] == "hold"
    assert task["blocking"] == 1
    r = post(client, f"/api/tasks/{task_id}/action", {"action": "queue"})
    assert r.status_code == 400 and "questions" in r.json()["error"]
    r = post(client, f"/api/tasks/{task_id}/answers", {"answers": {"0": "PostgreSQL"}})
    assert r.status_code == 200, r.text
    assert tasks.open_blocking(r.json()["task"]) == []
    assert "- Which database?\n  Answer: PostgreSQL" in r.json()["task"]["prompt"]
    r = post(client, f"/api/tasks/{task_id}/action", {"action": "queue"})
    assert r.status_code == 200
    assert tasks.next_queued(client.service.conn())["id"] == task_id


def test_a_failed_draft_holds_the_task_with_the_error(client, repo, fake_claude,
                                                      monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    fake_claude()
    task_id = post(client, "/api/tasks", {"description": "Do it",
                                          "project_dir": str(repo)}).json()["task"]["id"]
    task = drafted(client, task_id)
    assert task["status"] == "hold"
    assert task["draft_error"] == "Claude failed: usage limit"
    r = post(client, f"/api/tasks/{task_id}/action", {"action": "queue"})
    assert r.status_code == 400 and "no prompt" in r.json()["error"]
    # Retry the draft.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    r = post(client, f"/api/tasks/{task_id}/action", {"action": "redraft"})
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "drafting"
    task = drafted(client, task_id)
    assert task["status"] == "queued"
    assert task["draft_error"] is None
    r = post(client, f"/api/tasks/{task_id}/action", {"action": "redraft"})
    assert r.status_code == 400


def test_a_failed_draft_can_be_written_by_hand(client, repo, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    fake_claude()
    task_id = post(client, "/api/tasks", {"description": "Do it",
                                          "project_dir": str(repo)}).json()["task"]["id"]
    assert drafted(client, task_id)["draft_error"] == "Claude failed: something went wrong"
    r = client.patch(f"/api/tasks/{task_id}", json={"prompt": "## Goal\nDo it by hand."})
    assert r.json()["task"]["draft_error"] is None
    r = post(client, f"/api/tasks/{task_id}/action", {"action": "queue"})
    assert r.json()["task"]["status"] == "queued"


def test_answers_go_into_the_prompt(client, repo, fake_claude):
    fake_claude()
    task_id = post(client, "/api/tasks", {"description": "Make imports fast",
                                          "project_dir": str(repo)}).json()["task"]["id"]
    drafted(client, task_id)
    r = post(client, f"/api/tasks/{task_id}/answers", {"answers": {"0": "No, drop it.",
                                                                   "2": "Yes"}})
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["questions"][0]["answer"] == "No, drop it."
    assert task["prompt"] == (
        f"## Goal\nDone in {repo}.\n\n"
        "## Answers from the user\n\n"
        "These answers replace what the prompt above assumes about the same questions.\n\n"
        "- Keep the old format?\n  Answer: No, drop it.\n"
        "- Q3\n  Answer: Yes")
    # Answering again rewrites the section; clearing every answer removes it.
    r = post(client, f"/api/tasks/{task_id}/answers", {"answers": {"2": ""}})
    prompt = r.json()["task"]["prompt"]
    assert prompt.count("## Answers from the user") == 1
    assert "Q3" not in prompt and "No, drop it." in prompt
    r = post(client, f"/api/tasks/{task_id}/answers", {"answers": {"0": ""}})
    assert r.json()["task"]["prompt"] == f"## Goal\nDone in {repo}."
    r = post(client, f"/api/tasks/{task_id}/answers", {"answers": {"9": "x"}})
    assert r.status_code == 400


def test_drafts_left_over_are_resumed(client, repo, fake_claude):
    conn = client.service.conn()
    task = tasks.create(conn, "", repo, description="Make imports fast")
    assert task["status"] == "drafting"
    fake_claude()
    client.service.resume_drafts()
    assert drafted(client, task["id"])["status"] == "queued"


def test_a_task_needs_a_prompt_or_a_description(client, repo):
    r = post(client, "/api/tasks", {"description": " ", "project_dir": str(repo)})
    assert r.status_code == 400 and "prompt is empty" in r.json()["error"]
