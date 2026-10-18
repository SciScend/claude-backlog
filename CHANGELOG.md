# Changelog

## 0.1.0 - unreleased

First version.

- Board in the browser: On hold, Queue (High, Normal, Low), Running, Done and
  Needs attention; drag and drop; task panel with summary, attempts, live
  log, branch commits and diff; a two-step new-task form with a project
  folder picker and a prompt template (editable in Settings); settings.
- Add a task from a short description: it is on the board at once while
  Claude drafts the prompt in the background in one tool-less call, with
  an outline of the project gathered without a model (file list, README,
  CLAUDE.md, build and test commands, matching files; cached per git HEAD).
  The draft model and effort are their own settings (default Sonnet, low
  effort). Claude's questions come with the assumption the prompt makes;
  only a blocking question holds the task, answers go into the prompt
  without a new call, and a failed draft can be retried or written by hand.
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
- Tasks on the same project: a task can start after another one (it waits,
  then starts from that task's branch); the board checks finished branches
  for merge conflicts with their base branch and with each other.
- Uncommitted changes when a task is added: the new-task form offers
  "Commit now" (the changed files, a commit message, your hooks and git
  identity); the `/backlog` skill offers to commit them first; `add` prints
  a note.
- `/backlog` skill for Claude Code, CLI (`add`, `list`, `go`, `stop`,
  `export`, `report`), systemd user service and launcher (`install`).
- Run report and desktop notification when the queue stops.
- A calmer, more consistent look for the board, the task panel and the
  dialogs, in light and dark mode and on the phone: one scale for spacing,
  radii and type, softer borders and shadows, clear hover, pressed and focus
  states, and amber kept for the running task and the stop threshold only.
- The project folder picker lists a folder that is reachable under several
  paths through symlinks (say `~/projects` -> `/data/projects`) only once.
