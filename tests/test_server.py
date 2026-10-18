import pytest
from starlette.testclient import TestClient

from claude_backlog import settings, tasks
from claude_backlog.server import Service, create_app

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
    assert info == {"exists": True, "git": True, "branch": "main", "branches": ["main"],
                    "dirty": False}
    info = client.get("/api/project-info", params={"path": str(plain_dir)}).json()
    assert info["git"] is False
    assert client.get("/api/project-info", params={"path": "/no/such"}).json() == {
        "exists": False}
