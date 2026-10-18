"""Command-line interface for claude-backlog."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from claude_backlog import __version__, conflicts, db, gitops, report, settings, tasks

__all__ = ["build_parser", "main", "run_cli"]

EPILOG = """Examples:
  claude-backlog add -p high "Migrate src/api to the v2 client and fix the tests"
  claude-backlog add -C ~/projects/site task.md
  claude-backlog list
  claude-backlog go --until 07:30
  claude-backlog open
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-backlog",
        description="A backlog board that Claude Code works through while you are away.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version",
                        version=f"claude-backlog {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("serve", help="run the service in the foreground")
    p.add_argument("--port", type=int, default=None,
                   help="board port (default: from settings, 8765); the phone port "
                        "is the next one")

    p = sub.add_parser("install", help="install the service, the /backlog skill and "
                                       "a launcher")
    p.add_argument("--phone", action="store_true",
                   help="also make the board reachable from your phone through "
                        "tailscale serve")
    sub.add_parser("uninstall", help="remove what install added (keeps your tasks)")
    sub.add_parser("open", help="open the board in the browser")

    p = sub.add_parser("add", help="add a task")
    p.add_argument("source", help="the prompt, a task file (.md), or '-' for stdin")
    p.add_argument("-C", "--project", default=None,
                   help="project folder (default: the current folder, or the task "
                        "file's 'project')")
    p.add_argument("-t", "--title", default=None)
    p.add_argument("-p", "--priority", choices=["high", "normal", "low"], default=None)
    p.add_argument("--hold", action="store_true", help="put it on hold, not in the queue")
    p.add_argument("--after", metavar="ID", default=None,
                   help="start after this task of the same repository is done")
    p.add_argument("-m", "--model", default=None)
    p.add_argument("-e", "--effort", default=None,
                   choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("-w", "--max-windows", default=None,
                   help="5-hour windows one attempt may use: a number or 'auto'")

    p = sub.add_parser("list", help="show the queue and what needs attention")
    p.add_argument("-a", "--all", action="store_true", help="include done and archived")
    p.add_argument("-C", "--project", default=None, metavar="DIR",
                   help="only the tasks of this folder's repository that are not "
                        "merged yet, with the files their branches change")

    p = sub.add_parser("go", help="start working through the queue now")
    p.add_argument("--until", metavar="HH:MM", default=None,
                   help="start no new task after this time")
    p = sub.add_parser("stop", help="stop after the current task")
    p.add_argument("--now", action="store_true",
                   help="stop the current task too (it goes back to the queue)")

    p = sub.add_parser("export", help="print a task as a task file")
    p.add_argument("task_id")
    sub.add_parser("report", help="print the latest run report")
    sub.add_parser("template", help="print the prompt template for new tasks")
    return parser


# --- talking to the service ------------------------------------------------------


def _port(conn) -> int:
    return int(settings.get(conn, "port"))


def _api(conn, method, path, data=None) -> dict:
    url = f"http://127.0.0.1:{_port(conn)}{path}"
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(url, data=body if method != "GET" else None,
                                 method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        with contextlib.suppress(ValueError):
            detail = json.loads(detail).get("error", detail)
        raise SystemExit(f"claude-backlog: {detail}") from None
    except OSError:
        raise SystemExit(
            "claude-backlog: the service is not running. Start it with "
            "'claude-backlog install' (or 'claude-backlog serve' in a terminal)."
        ) from None


# --- commands ----------------------------------------------------------------------


def cmd_add(args, conn) -> int:
    source = args.source
    default_project = args.project
    if source == "-":
        text = sys.stdin.read()
        kwargs = tasks.parse_task_file(text, default_project or str(Path.cwd()))
    elif source.endswith(".md") and Path(source).is_file():
        path = Path(source).resolve()
        info = gitops.repo_info(path.parent)
        fallback = default_project or (info.root if info else str(path.parent))
        kwargs = tasks.parse_task_file(path.read_text(encoding="utf-8"), fallback)
        if args.project:
            kwargs["project_dir"] = args.project
    else:
        kwargs = {"prompt": source, "project_dir": default_project or str(Path.cwd())}
    for key in ("title", "priority", "model", "effort", "max_windows"):
        value = getattr(args, key)
        if value:
            kwargs[key] = value
    if args.hold:
        kwargs["status"] = "hold"
    if args.after:
        kwargs["after_task"] = args.after
    task = tasks.create(conn, **kwargs)
    where = "on hold" if task["status"] == "hold" else f"queued, {task['priority_name']}"
    print(f"Added {task['id']}: {task['title']} ({where})")
    print(f"  project: {task['project_dir']}"
          + ("" if task["in_git"] else "  (not a git repository: runs in place)"))
    if task["after_task"]:
        print(f"  starts after: {task['after_task']}")
    if task["in_git"] and gitops.is_dirty(task["repo_root"]):
        print(f"claude-backlog: note: {task['repo_root']} has uncommitted changes; they "
              "are not part of the task's starting point (commit them first if the "
              "task needs them).", file=sys.stderr)
    return 0


# How many changed files `list -C` shows per task.
LIST_FILES = 15


def _list_unmerged(folder, conn) -> int:
    """The tasks of a repository that are not merged yet (the /backlog skill
    checks these before it adds a task)."""
    path = Path(folder).expanduser()
    info = gitops.repo_info(path) if path.is_dir() else None
    if info is None:
        print(f"{folder} is not a git repository, so its tasks have no branches.")
        return 0
    cards = [t for t in tasks.list_tasks(conn) if t["repo_root"] == info.root]
    conflicts.annotate(cards)
    cards = [c for c in cards if not c["merged"]]
    if not cards:
        print(f"No tasks of {info.root} are waiting to be merged.")
        return 0
    print(f"Not merged yet in {info.root}:")
    for c in cards:
        extra = f"  (after {c['after_task']})" if c["after_task"] else ""
        print(f"  {c['id']:<36} {c['status']:<9} {c['title']}{extra}")
        if not (c["branch"] and c["base_commit"]
                and gitops.branch_exists(info.root, c["branch"])):
            continue
        files = gitops.changed_names(info.root, c["base_commit"], c["branch"])
        if files:
            more = len(files) - LIST_FILES
            print(f"      changes: {', '.join(files[:LIST_FILES])}"
                  + (f" and {more} more" if more > 0 else ""))
    print("Read a task's prompt with: claude-backlog export ID")
    return 0


def cmd_list(args, conn) -> int:
    if args.project:
        return _list_unmerged(args.project, conn)
    rows = tasks.list_tasks(conn, include_archived=args.all)
    groups = [("Running", ["running"]), ("Queue", ["queued"]), ("On hold", ["hold"]),
              ("Claude is writing the prompt", ["drafting"]),
              ("Needs attention", ["attention"])]
    if args.all:
        groups += [("Done", ["done"]), ("Archived", ["archived"])]
    for name, statuses in groups:
        items = [t for t in rows if t["status"] in statuses]
        if not items:
            continue
        print(f"{name}:")
        for t in items:
            extra = f"  [{t['note']}]" if t["note"] else ""
            prio = f"{t['priority_name']:<6} " if t["status"] == "queued" else ""
            print(f"  {prio}{t['id']:<36} {t['title']}{extra}")
    if not rows:
        print("The backlog is empty. Add a task with: claude-backlog add \"...\"")
    return 0


def cmd_export(args, conn) -> int:
    print(tasks.format_task_file(tasks.get(conn, args.task_id)), end="")
    return 0


def cmd_report(args, conn) -> int:
    path = report.latest()
    if not path:
        print("No reports yet.")
        return 0
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_template(args, conn) -> int:
    template = settings.get(conn, "prompt_template").strip()
    print(template or "No prompt template is set (Settings on the board).")
    return 0


def cmd_go(args, conn) -> int:
    result = _api(conn, "POST", "/api/run", {"until": args.until})
    print("Started." if result["started"] else "Already running.")
    return 0


def cmd_stop(args, conn) -> int:
    result = _api(conn, "POST", "/api/stop", {"now": args.now})
    if not result["stopped"]:
        print("Nothing is running.")
    else:
        print("Stopping now." if args.now else "Stopping after the current task.")
    return 0


def cmd_open(args, conn) -> int:
    url = f"http://127.0.0.1:{_port(conn)}/"
    if not webbrowser.open(url):
        print(url)
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    if args.command == "serve":
        from claude_backlog.server import serve

        serve(port=args.port)
        return 0
    if args.command in ("install", "uninstall"):
        from claude_backlog import install

        if args.command == "uninstall":
            return install.uninstall()
        conn = db.connect()
        port = _port(conn)
        conn.close()
        return install.install(port, phone=args.phone)
    conn = db.connect()
    handlers = {"add": cmd_add, "list": cmd_list, "export": cmd_export,
                "report": cmd_report, "template": cmd_template, "go": cmd_go,
                "stop": cmd_stop, "open": cmd_open}
    try:
        return handlers[args.command](args, conn)
    except tasks.TaskError as exc:
        print(f"claude-backlog: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def run_cli() -> None:
    sys.exit(main())
