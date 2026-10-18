# Changelog

## 0.1.0 - unreleased

First version.

- Board in the browser: On hold, Queue (High, Normal, Low), Running, Done and
  Needs attention; drag and drop; task panel with summary, attempts, live
  log, branch commits and diff; new-task form with a prompt template
  (editable in Settings) and "Write the prompt with Claude", which drafts
  the prompt from a short description; settings.
- Phone layout with tabs and move buttons; phone access through
  `tailscale serve`, limited to one Tailscale login.
- Runner: one task at a time through claude-nightshift, each in its own git
  worktree on `backlog/<id>`; exit code mapping, attempts, a fair turn for
  every queued task within a run, recovery after a crash, `systemd-inhibit`
  while the queue runs.
- Starts on Run now, or when the screensaver has been on for a set time
  (Cinnamon, GNOME, freedesktop screensavers, logind fallback); stops when you
  come back.
- Weekly limit setting (default 50%) passed to nightshift.
- `/backlog` skill for Claude Code, CLI (`add`, `list`, `go`, `stop`,
  `export`, `report`), systemd user service and launcher (`install`).
- Run report and desktop notification when the queue stops.
