"""The service: the board, its JSON API, the runner and the away watcher."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from markdown_it import MarkdownIt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from claude_backlog import (
    __version__,
    conflicts,
    db,
    draft,
    gitops,
    paths,
    report,
    runner,
    settings,
    tasks,
)
from claude_backlog.presence import Watcher
from claude_backlog.prompt import decisions_count
from claude_backlog.runner import Runner
from claude_backlog.usage import UsageCache

__all__ = ["create_app", "serve", "Service"]

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
LOG_CHUNK = 200_000
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

_markdown = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


class Service:
    """Everything the API needs, created once per process."""

    def __init__(self, db_path=None, runner_=None, watcher=None, usage=None, port=8765):
        self.db_path = db_path
        self.port = port
        self.phone_port = port + 1
        self.runner = runner_ or Runner(db_path)
        self.watcher = watcher
        self.usage = usage or UsageCache()
        self._local = threading.local()
        self._conns = []
        self._conns_lock = threading.Lock()

    def conn(self):
        """A connection for the calling thread (API calls run in a thread pool)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self.db_path)
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._conns:
                conn.close()
            self._conns.clear()


# --- helpers -------------------------------------------------------------------


def _json(request_body: bytes) -> dict:
    if not request_body:
        return {}
    try:
        data = json.loads(request_body)
    except ValueError:
        raise tasks.TaskError("the request is not valid JSON") from None
    if not isinstance(data, dict):
        raise tasks.TaskError("the request must be a JSON object")
    return data


def _until(value) -> float | None:
    """'HH:MM' -> the next time it is that o'clock, as epoch seconds."""
    if not value:
        return None
    try:
        hour, minute = (int(x) for x in str(value).split(":"))
        now = datetime.datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        raise tasks.TaskError("until must be HH:MM") from None
    if target <= now:
        target += datetime.timedelta(days=1)
    return target.timestamp()


def _summary_path(task_id) -> Path:
    return paths.task_dir(task_id) / "SUMMARY.md"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _commit_count(task) -> int:
    if not (task["branch"] and task["base_commit"] and task["repo_root"]):
        return 0
    out = gitops.git("rev-list", "--count", f"{task['base_commit']}..{task['branch']}",
                     cwd=task["repo_root"], check=False)
    return int(out) if out.isdigit() else 0


def _card(task) -> dict:
    card = dict(task)
    card["decisions"] = 0
    card["commits"] = 0
    card["has_summary"] = False
    if task["status"] in ("done", "attention", "queued") and task["attempts"]:
        summary = _read(_summary_path(task["id"]))
        if summary is not None:
            card["has_summary"] = True
            card["decisions"] = decisions_count(summary)
        card["commits"] = _commit_count(task)
    card["new"] = bool(task["finished_at"] and (task["seen_at"] or 0) < task["finished_at"])
    return card


def _cards(conn) -> list[dict]:
    """All cards, with "start after" and the merge check filled in."""
    cards = [_card(t) for t in tasks.list_tasks(conn)]
    by_id = {c["id"]: c for c in cards}
    for card in cards:
        card["after_title"] = None
        card["waiting"] = False
        if not card["after_task"]:
            continue
        before = by_id.get(card["after_task"])
        if before is None:  # archived
            with contextlib.suppress(tasks.TaskError):
                before = tasks.get(conn, card["after_task"])
        if before is not None:
            card["after_title"] = before["title"]
            card["waiting"] = (before["status"] not in tasks.FINISHED
                               and card["status"] in ("queued", "hold"))
    conflicts.annotate(cards)
    return cards


def _gsetting(schema, key):
    if not shutil.which("gsettings"):
        return None
    try:
        out = subprocess.run(["gsettings", "get", schema, key], capture_output=True,
                             text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip().split()[-1].strip("'")


def power_warnings(away_minutes) -> list[str]:
    """Warn when the machine would suspend before the away delay is reached."""
    warnings = []
    for desktop in ("cinnamon", "gnome"):
        idle = _gsetting(f"org.{desktop}.desktop.session", "idle-delay")
        if idle is None:
            continue
        delay = int(idle) + away_minutes * 60 if idle.isdigit() else away_minutes * 60
        for source in ("ac", "battery"):
            kind = _gsetting(f"org.{desktop}.settings-daemon.plugins.power",
                             f"sleep-inactive-{source}-type")
            timeout = _gsetting(f"org.{desktop}.settings-daemon.plugins.power",
                                f"sleep-inactive-{source}-timeout")
            if kind in (None, "nothing", "blank") or not (timeout or "").isdigit():
                continue
            if 0 < int(timeout) < delay:
                warnings.append(
                    f"On {source.upper()} power the computer suspends after "
                    f"{int(timeout) // 60} min without input, before the queue would "
                    f"start (about {delay // 60} min). Raise the suspend time or "
                    f"lower 'start when away after'."
                )
        break
    return warnings


def _state(svc: Service) -> dict:
    conn = svc.conn()
    conf = settings.get_all(conn)
    warnings = []
    if not runner.nightshift_available():
        warnings.append("claude-nightshift was not found, so tasks cannot run.")
    if not svc.runner.inhibitor.available:
        warnings.append("systemd-inhibit was not found: the computer may go to sleep "
                        "during a run.")
    if conf["away_enabled"]:
        warnings += power_warnings(conf["away_minutes"])
        if svc.watcher and svc.watcher.detector is None and svc.watcher.read_at:
            warnings.append("Away detection does not work on this desktop; use Run now.")
    return {
        "version": __version__,
        "revision": db.revision(conn),
        "tasks": _cards(conn),
        "runner": svc.runner.status(),
        "usage": svc.usage.get(),
        "presence": svc.watcher.status() if svc.watcher else None,
        "settings": conf,
        "default_prompt_template": settings.PROMPT_TEMPLATE,
        "warnings": warnings,
        "now": time.time(),
    }


def _detail(svc: Service, task_id) -> dict:
    conn = svc.conn()
    task = next((c for c in _cards(conn) if c["id"] == task_id), None)
    if task is None:  # archived
        task = _card(tasks.get(conn, task_id))
        task.update(after_title=None, waiting=False, merged=False, conflicts=[])
    summary = _read(_summary_path(task_id))
    runs = [dict(r) for r in conn.execute(
        "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC", (task_id,))]
    detail = {
        "task": task,
        "summary_html": _markdown.render(summary) if summary else None,
        "state_md": _read(paths.task_dir(task_id) / "STATE.md"),
        "runs": runs,
        "commits": [],
        "files": [],
        "task_dir": str(paths.task_dir(task_id)),
        "merge_command": None,
    }
    if task["branch"] and task["repo_root"]:
        detail["commits"] = gitops.commits(task["repo_root"], task["base_commit"],
                                           task["branch"])
        detail["files"] = gitops.diff_stat(task["repo_root"], task["base_commit"],
                                           task["branch"])
        detail["merge_command"] = f"git -C {task['repo_root']} merge {task['branch']}"
    return detail


# --- endpoints -----------------------------------------------------------------


def _handler(fn):
    async def endpoint(request: Request):
        try:
            body = await request.body() if request.method in UNSAFE else b""
            result = await asyncio.to_thread(fn, request, _json(body))
        except tasks.TaskError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except gitops.GitError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except draft.DraftError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        if isinstance(result, Response):
            return result
        return JSONResponse(result)
    return endpoint


def _svc(request) -> Service:
    return request.app.state.service


def get_state(request, _):
    return _state(_svc(request))


def create_task(request, data):
    svc = _svc(request)
    allowed = {"prompt", "project_dir", "title", "priority", "status", "model", "effort",
               "max_windows", "threshold", "max_attempts", "base_branch", "after_task"}
    task = tasks.create(svc.conn(), **{k: v for k, v in data.items() if k in allowed})
    return {"task": task}


def write_draft(request, data):
    conf = settings.get_all(_svc(request).conn())
    return draft.draft(str(data.get("description") or ""),
                       str(data.get("project_dir") or ""),
                       conf["prompt_template"], conf["draft_model"],
                       conf["draft_effort"])


def get_task(request, _):
    return _detail(_svc(request), request.path_params["task_id"])


def patch_task(request, data):
    task_id = request.path_params["task_id"]
    return {"task": tasks.update(_svc(request).conn(), task_id, data)}


def move_task(request, data):
    task = tasks.move(_svc(request).conn(), request.path_params["task_id"],
                      data.get("status", "queued"), data.get("priority"), data.get("index"))
    return {"task": task}


def task_action(request, data):
    svc = _svc(request)
    conn = svc.conn()
    task_id = request.path_params["task_id"]
    action = data.get("action")
    task = tasks.get(conn, task_id)
    if action == "run-next":
        tasks.move(conn, task_id, "queued", 1, 0)
    elif action == "hold":
        tasks.move(conn, task_id, "hold")
    elif action in ("queue", "retry"):
        tasks.move(conn, task_id, "queued")
    elif action == "seen":
        tasks.mark_seen(conn, task_id)
    elif action == "archive":
        if task["status"] == "running":
            raise tasks.TaskError("the task is running; stop it first")
        if task["worktree"] and task["repo_root"]:
            gitops.remove_worktree(task["repo_root"], task["worktree"])
        tasks.set_status(conn, task_id, "archived", worktree=None)
    elif action == "delete-branch":
        if task["status"] not in ("archived", "done", "attention", "hold"):
            raise tasks.TaskError("only a finished, held or archived task's branch "
                                  "can be deleted")
        if task["worktree"] and task["repo_root"]:
            gitops.remove_worktree(task["repo_root"], task["worktree"])
        if task["branch"] and task["repo_root"]:
            gitops.delete_branch(task["repo_root"], task["branch"])
        tasks.set_status(conn, task_id, task["status"], worktree=None, branch=None,
                         base_commit=None, base_ref=None, attempts=0)
    elif action == "delete":
        if task["worktree"] and task["repo_root"]:
            gitops.remove_worktree(task["repo_root"], task["worktree"])
        tasks.delete(conn, task_id)
        return {"deleted": task_id}
    else:
        raise tasks.TaskError(f"unknown action {action!r}")
    return {"task": tasks.get(conn, task_id)}


def task_diff(request, _):
    task = tasks.get(_svc(request).conn(), request.path_params["task_id"])
    if not (task["branch"] and task["repo_root"]):
        return PlainTextResponse("")
    return PlainTextResponse(gitops.diff(task["repo_root"], task["base_commit"],
                                         task["branch"]))


def task_log(request, _):
    conn = _svc(request).conn()
    task_id = request.path_params["task_id"]
    run_id = request.query_params.get("run")
    if run_id:
        row = conn.execute("SELECT * FROM runs WHERE id = ? AND task_id = ?",
                           (run_id, task_id)).fetchone()
    else:
        row = conn.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY started_at "
                           "DESC LIMIT 1", (task_id,)).fetchone()
    if row is None or not row["log_path"]:
        return {"run": None, "text": "", "offset": 0}
    offset = int(request.query_params.get("offset", 0) or 0)
    path = Path(row["log_path"])
    text = ""
    try:
        size = path.stat().st_size
        if offset == 0 and size > LOG_CHUNK:
            offset = size - LOG_CHUNK
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(LOG_CHUNK)
        text = data.decode("utf-8", errors="replace")
        offset += len(data)
    except OSError:
        pass
    return {"run": row["id"], "text": text, "offset": offset,
            "running": row["ended_at"] is None}


def start_run(request, data):
    svc = _svc(request)
    started = svc.runner.start("run-now", _until(data.get("until")))
    return {"started": started, "runner": svc.runner.status()}


def stop_run(request, data):
    svc = _svc(request)
    stopped = svc.runner.stop(now=bool(data.get("now")))
    return {"stopped": stopped, "runner": svc.runner.status()}


def put_settings(request, data):
    try:
        return {"settings": settings.update(_svc(request).conn(), data)}
    except ValueError as exc:
        raise tasks.TaskError(str(exc)) from None


def refresh_usage(request, _):
    return _svc(request).usage.get(force=True)


# A folder holding one of these is offered as a project.
PROJECT_MARKERS = (".git", "pyproject.toml", "setup.py", "package.json", "Cargo.toml",
                   "go.mod", "Makefile", "CMakeLists.txt", "pom.xml", "build.gradle",
                   "Gemfile", "composer.json", "CLAUDE.md", "AGENTS.md")
SKIP_FOLDERS = {"node_modules", "__pycache__", "venv", "site-packages"}


def _is_project(path: Path) -> bool:
    return any((path / marker).exists() for marker in PROJECT_MARKERS)


def _subfolders(path: Path) -> list[Path]:
    try:
        return sorted((p for p in path.iterdir() if not p.name.startswith(".")
                       and p.name not in SKIP_FOLDERS and p.is_dir()),
                      key=lambda p: p.name.lower())
    except OSError:
        return []


def find_projects(roots) -> list[str]:
    """Project folders up to two levels below each root."""
    found = []
    for root in roots:
        for child in _subfolders(root):
            if _is_project(child):
                found.append(str(child))
                continue
            found += [str(p) for p in _subfolders(child) if _is_project(p)]
    return list(dict.fromkeys(found))


def list_projects(request, _):
    conn = _svc(request).conn()
    conf = settings.get_all(conn)
    recent = [r["project_dir"] for r in conn.execute(
        "SELECT project_dir, MAX(created_at) AS last FROM tasks GROUP BY project_dir "
        "ORDER BY last DESC LIMIT 10")]
    roots = [Path(os.path.expanduser(r)) for r in conf["project_roots"]]
    roots += [Path(r).parent for r in recent]
    return {"recent": recent, "folders": find_projects(dict.fromkeys(roots)),
            "home": str(Path.home())}


def list_folders(request, _):
    """The subfolders of one folder, for typing a path in the folder picker."""
    folder = request.query_params.get("path", "")
    path = Path(os.path.expanduser(folder or "~"))
    if not path.is_dir():
        return {"path": str(path), "folders": []}
    return {"path": str(path),
            "folders": [{"path": str(p), "project": _is_project(p)}
                        for p in _subfolders(path)[:500]]}


def project_info(request, _):
    folder = request.query_params.get("path", "")
    path = Path(os.path.expanduser(folder))
    if not folder or not path.is_dir():
        return {"exists": False}
    info = gitops.repo_info(path)
    branches = []
    if info:
        out = gitops.git("for-each-ref", "--format=%(refname:short)", "refs/heads",
                         cwd=info.root, check=False)
        branches = [b for b in out.splitlines() if not b.startswith("backlog/")]
    return {
        "exists": True,
        "git": bool(info),
        "root": info.root if info else str(path.resolve()),
        "branch": info.branch if info else None,
        "branches": branches,
        "dirty": gitops.is_dirty(path) if info else False,
    }


def latest_report(request, _):
    path = report.latest()
    return {"path": str(path) if path else None, "text": _read(path) if path else None}


def phone_info(request, _):
    svc = _svc(request)
    url = None
    if shutil.which("tailscale"):
        try:
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                                 text=True, timeout=5)
            data = json.loads(out.stdout or "{}")
            name = (data.get("Self") or {}).get("DNSName", "").rstrip(".")
            if name:
                url = f"https://{name}"
            login = None
            users = data.get("User") or {}
            uid = str((data.get("Self") or {}).get("UserID", ""))
            if uid in users:
                login = users[uid].get("LoginName")
            return {"url": url, "login": login, "backend": data.get("BackendState"),
                    "port": svc.phone_port}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    return {"url": None, "login": None, "backend": None, "port": svc.phone_port}


def phone_qr(request, _):
    import io

    import qrcode
    import qrcode.image.svg

    info = phone_info(request, None)
    if not info["url"]:
        return Response(status_code=404)
    out = io.BytesIO()
    qrcode.make(info["url"], image_factory=qrcode.image.svg.SvgPathImage,
                border=1).save(out)
    return Response(out.getvalue(), media_type="image/svg+xml")


async def events(request: Request):
    svc = _svc(request)

    def signature():
        conn = svc.conn()
        presence = svc.watcher.status() if svc.watcher else {}
        away = presence.get("away_seconds")
        return (db.revision(conn), svc.runner.version, svc.usage.read_at,
                presence.get("detector"), bool(away))

    async def stream():
        last = None
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            current = await asyncio.to_thread(signature)
            if current != last:
                last = current
                idle = 0
                yield "event: change\ndata: {}\n\n"
            else:
                idle += 1
                if idle >= 15:
                    idle = 0
                    yield ": keep-alive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


async def index(request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


# --- access control ------------------------------------------------------------


class AccessGuard:
    """Who may use the board, depending on which port the request came in on.

    The local port serves this computer only: the Host header must name
    localhost (against DNS rebinding) and a browser request that changes
    something must come from the board itself (against other web pages).
    The phone port is only reached through ``tailscale serve``, which adds the
    caller's login; it must be the one set in Settings.
    """

    def __init__(self, app, service: Service):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope["headers"]}
        server_port = (scope.get("server") or (None, None))[1]
        error = await asyncio.to_thread(self.check, scope, headers, server_port)
        if error:
            response = PlainTextResponse(error, status_code=403)
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)

    def check(self, scope, headers, server_port) -> str | None:
        host = headers.get("host", "")
        origin = headers.get("origin")
        method = scope["method"]
        if server_port == self.service.phone_port:
            login = settings.get(self.service.conn(), "tailscale_login")
            caller = headers.get("tailscale-user-login", "")
            if not login:
                return ("Phone access is not set up: enter your Tailscale login in the "
                        "board's Settings on the computer.")
            if caller.lower() != login.lower():
                return "This Tailscale login is not allowed to use this board."
            if method in UNSAFE and origin and origin != f"https://{host}":
                return "Cross-site request refused."
            return None
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host
        if hostname not in ("127.0.0.1", "localhost", "[::1]"):
            return "The board answers only on localhost."
        if method in UNSAFE:
            if origin and origin != f"http://{host}":
                return "Cross-site request refused."
            if "application/json" not in headers.get("content-type", ""):
                return "Requests that change something must send JSON."
        return None


# --- app and service -----------------------------------------------------------


def create_app(service: Service, on_shutdown=None) -> Starlette:
    h = _handler
    routes = [
        Route("/", index),
        Route("/api/state", h(get_state)),
        Route("/api/events", events),
        Route("/api/tasks", h(create_task), methods=["POST"]),
        Route("/api/draft", h(write_draft), methods=["POST"]),
        Route("/api/tasks/{task_id}", h(get_task)),
        Route("/api/tasks/{task_id}", h(patch_task), methods=["PATCH"]),
        Route("/api/tasks/{task_id}/move", h(move_task), methods=["POST"]),
        Route("/api/tasks/{task_id}/action", h(task_action), methods=["POST"]),
        Route("/api/tasks/{task_id}/diff", h(task_diff)),
        Route("/api/tasks/{task_id}/log", h(task_log)),
        Route("/api/run", h(start_run), methods=["POST"]),
        Route("/api/stop", h(stop_run), methods=["POST"]),
        Route("/api/settings", h(put_settings), methods=["PUT"]),
        Route("/api/usage", h(refresh_usage), methods=["POST"]),
        Route("/api/projects", h(list_projects)),
        Route("/api/folders", h(list_folders)),
        Route("/api/project-info", h(project_info)),
        Route("/api/report", h(latest_report)),
        Route("/api/phone", h(phone_info)),
        Route("/api/phone/qr.svg", h(phone_qr)),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        if on_shutdown:
            # uvicorn re-raises SIGTERM after this, so cleanup cannot wait for
            # server.run() to return.
            await asyncio.to_thread(on_shutdown)

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.service = service
    guarded = AccessGuard(app, service)
    guarded.state = app.state  # for tests that reach in
    return guarded


def _socket(port) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.set_inheritable(True)
    return sock


def serve(port=None, db_path=None) -> None:
    """Run the service until it is stopped (what the systemd unit runs)."""
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = db.connect(db_path)
    port = port or settings.get(conn, "port")
    recovered = runner.recover(conn)
    if recovered:
        logger.info("put back in the queue after a crash: %s", ", ".join(recovered))
    conn.close()

    service = Service(db_path=db_path, port=port)
    service.watcher = Watcher(service.runner, db_path)
    service.watcher.start()
    def shutdown():
        service.watcher.stop()
        if service.runner.state != "idle":
            logger.info("stopping the running task; it goes back to the queue")
        service.runner.shutdown()
        service.close()
        logger.info("stopped")

    app = create_app(service, on_shutdown=shutdown)
    config = uvicorn.Config(app, log_level="warning", lifespan="on",
                            timeout_graceful_shutdown=3)
    server = uvicorn.Server(config)
    sockets = [_socket(port), _socket(port + 1)]
    logger.info("claude-backlog %s: board at http://127.0.0.1:%s (phone port %s)",
                __version__, port, port + 1)
    server.run(sockets=sockets)
