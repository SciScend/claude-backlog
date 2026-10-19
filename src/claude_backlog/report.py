"""The report written each time the queue stops."""

from __future__ import annotations

import datetime
from pathlib import Path

from claude_backlog import paths, tasks

__all__ = ["write", "summary_line", "latest", "fmt_duration"]

SUMMARY_LINES = 12


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _fmt(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def summary_line(reason, attempts) -> str:
    done = sum(1 for a in attempts if a.result == "done")
    attention = sum(1 for a in attempts if a.status == "attention")
    parts = [f"Stopped: {reason}."]
    if attempts:
        parts.append(f"{done} done, {attention} need attention, "
                     f"{len(attempts) - done - attention} back in the queue.")
    return " ".join(parts)


def write(conn, started, ended, reason, trigger, attempts) -> Path:
    how = "you were away" if trigger == "away" else "Run now"
    lines = [
        f"# Backlog run {_fmt(started)} - {_fmt(ended)[11:]}",
        "",
        f"Started because: {how}. Stopped: {reason}.",
        "",
        "| Task | Result | Exit | Time | Spent | Commits | Log |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in attempts:
        result = a.result + (f" ({a.note})" if a.note and a.result != "done" else "")
        exit_code = "-" if a.exit_code is None else str(a.exit_code)
        spent = "-" if a.spent is None else f"{a.spent:g}%"
        lines.append(f"| {a.title} (`{a.task_id}`) | {result} | {exit_code} | "
                     f"{fmt_duration(a.ended_at - a.started_at)} | {spent} | "
                     f"{a.commits} | {a.log_path or '-'} |")
    for a in attempts:
        if a.result != "done":
            continue
        summary = paths.task_dir(a.task_id) / "SUMMARY.md"
        lines += ["", f"## {a.title}", ""]
        try:
            text = summary.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            text = ["(SUMMARY.md could not be read)"]
        # Quoted, so the summary's own headings do not break the report's.
        lines += [f"> {line}".rstrip() for line in text[:SUMMARY_LINES]]
        if len(text) > SUMMARY_LINES:
            lines.append("> ...")
        lines += ["", f"Full summary: {summary}"]
    pending = tasks.queue(conn)
    lines += ["", "## Still in the queue", ""]
    if pending:
        lines += [f"- {t['title']} (`{t['id']}`, {t['priority_name']})" for t in pending]
    else:
        lines.append("Nothing.")
    folder = paths.reports_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.datetime.fromtimestamp(started):%Y-%m-%d-%H%M%S}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def latest() -> Path | None:
    folder = paths.reports_dir()
    if not folder.is_dir():
        return None
    reports = sorted(folder.glob("*.md"))
    return reports[-1] if reports else None
