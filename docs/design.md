# claude-backlog - design

Status: reviewed (2026-10-18). Phase 1 is being implemented.
Nothing here is implemented.

This version replaces the CLI-only draft of 2026-10-17. It keeps what was
good there (claude-nightshift as the engine, the unattended preamble,
`STATE.md` and `SUMMARY.md`, the exit code mapping, `systemd-inhibit`) and
puts a board in the browser at the center.

Decided so far:

- The queue starts in two ways: when I press **Run now**, or **when I am
  away**, i.e. when the screensaver has been on for a while (section 4).
  There are no run windows or timers.
- The default stop threshold is **50%** of the weekly limit.
- Every task in a git repository runs on **its own branch, in its own git
  worktree**, from the first version on.
- The board can also be opened **from the phone**, from anywhere, through
  Tailscale (section 3, "Access").
- **Open source** on GitHub (MIT), written so that anyone with Claude Code
  on Linux can install and use it, not only on the author's machine
  (section 12).

## 1. Goal

Use Claude as fully as possible while I am away from the computer.

- When I have a long task that Claude can do on its own, I put it in the
  backlog with its prompt and a priority.
- When I leave, I press "Run now", or I just walk away and the queue starts
  by itself once the computer notices I am gone. Claude works through the
  backlog, one task at a time, and stops when weekly usage reaches the
  percentage I set, when the queue is empty, when I come back (if it
  started by itself), or when I stop it.
- A board in the browser shows every task. On it I add tasks, change their
  priority by dragging, watch the running one and read the results.
- Each task's work lands on its own branch, so I review it as a diff and
  merge what I want. My working copy is never touched.

## 2. Overview

One program, `claude-backlog` (Python, uv). It runs as a systemd user
service (`claude-backlog serve`) that does three things:

```
                 browser (http://127.0.0.1:8765)
                          |
+-------------------------v--------------------------+
| claude-backlog serve  (systemd user service)       |
|                                                    |
|   web UI + JSON API  <-->  SQLite backlog.db       |
|        | Run now / Stop       ^                    |
|        v                      |                    |
|   runner ---------------------+                    |
|        ^ away / back                               |
|   presence watcher (screensaver state)             |
+--------|-------------------------------------------+
         | one task at a time
         v
  git worktree add -b backlog/<id> ~/.local/share/claude-backlog/worktrees/<id>
  systemd-inhibit claude-nightshift -f prompt -t 50 ...   (cwd: the worktree)
         |
         v
   claude -p ...
```

- **Web UI and API**: the board (section 3).
- **Runner**: when started, it takes the top task, prepares its worktree,
  builds its prompt, runs it through claude-nightshift, records the result,
  and goes on with the next task until a stop condition (section 4).
- **Presence watcher**: notices when I am away and when I am back, and
  starts or stops the runner accordingly (section 4).

Everything lives in one data folder, by default
`~/.local/share/claude-backlog/` (`$XDG_DATA_HOME/claude-backlog`, or
`$CLAUDE_BACKLOG_HOME` when set):

```
~/.local/share/claude-backlog/
  backlog.db                  tasks, runs, settings (SQLite)
  tasks/<id>/                 per task, outside the project:
    prompt.md                   the last built prompt
    STATE.md                    progress across attempts (written by Claude)
    SUMMARY.md                  the result and done signal (written by Claude)
    nightshift-status.md        nightshift's status file
    logs/<run>.log              nightshift --log-file output, one per attempt
  worktrees/<id>/             the task's git worktree
  reports/YYYY-MM-DD-HHMMSS.md  one report per run of the queue
```

Keeping the task's own files in `tasks/<id>/`, outside the worktree, means
they never end up in a commit and survive when the worktree is removed.

Other ways in, for when the browser is not the easiest:

- CLI: `claude-backlog add`, `list`, `go`, `stop` (section 9).
- A Claude Code skill, `/backlog`, that turns the task being discussed in
  the current session into a backlog task (section 7).

### Why a service

The board should be there whenever I open it, like a bookmark, without
starting anything first, and something has to watch for "away" all the
time. One long-running user service does both. It is wanted by
`default.target`, not `graphical-session.target`: some desktops (Cinnamon,
checked 2026-10-18) never start the latter, and the screensaver is reachable
on the user's session bus either way. The unit uses `KillMode=mixed`, so on
shutdown the service stops the running task itself (it goes back to the
queue) before anything else is killed. `claude-backlog install` writes and
enables the unit.

### Why claude-nightshift stays

nightshift already does the hard part of running one task: waiting out
5-hour limits, resuming, and stopping at a weekly threshold. It is tested
and useful on its own. Backlog calls it once per task and acts on its exit
code; it does not decide anything from usage figures itself.

Backlog needs small changes in nightshift (made there, not worked around
here):

1. `--usage-json`: print current usage (7-day %, 5-hour %, reset times)
   and exit. The board shows it; nothing is decided from it.
2. `--status-file PATH`: put the status file where backlog says
   (`tasks/<id>/nightshift-status.md`) instead of the working directory.
3. A separate exit code, `5`, for "usage unreadable" (today it is part of
   exit `1`). It is an environment problem, not a task failure, so backlog
   stops the queue on it instead of failing every task in turn.

## 3. The board

```
+------------------------------------------------------------------------------+
| Claude Backlog   Week [##########--|-------] 38%  stop at 50%  resets Thu 09:00|
| Idle. Starts when away: ON (15 min)     [New task] [Run now v] [Settings]     |
+------------------+----------------------+-------------------+-----------------+
| ON HOLD          | QUEUE                | RUNNING           | DONE            |
|                  | -- High ------------ |                   |  * new          |
| [Refactor CSS]   | [Migrate API to v2]  | [Course anim. 7]  | [Docs cleanup]  |
|  site / draft    |  acme-app  opus/high |  teaching         |  done 05:12     |
|                  | [Fix flaky tests]    |  1h20m, window 1  |  4 commits      |
|                  | -- Normal ---------- |  > running tests  |  3 decisions    |
|                  | [Write README]       |                   |                 |
|                  | -- Low ------------- |                   | NEEDS ATTENTION |
|                  | [Rename variables]   |                   | [Lint fixes]    |
|                  |                      |                   |  failed, exit 1 |
+------------------+----------------------+-------------------+-----------------+
```

"New task" is in the header (on the phone, a button at the bottom of the
screen). An empty queue says so and offers the same button. The empty
drop zones ("Drag a task here") show only while a card is being dragged.

### Columns

- **On hold**: tasks that exist but must not run yet (drafts, waiting for
  something, or waiting for an answer to a blocking question). The new
  task form can put a task here.
- **Queue**: what will run, in the order shown, top first. A task marked
  "waits for ..." is skipped until the task it starts after is done
  (section 6, "Tasks on the same project"). It has three priority groups:
  High, Normal, Low. Dragging a card moves it within a group or into
  another group; that is how priority is changed. Arrows on the card ("to
  top", "up", "down") do the same without dragging.
- **Running**: the task being worked on: time so far, which 5-hour window,
  and the last lines of its log, updated live.
- **Done**: finished tasks, newest first. Tasks finished since I last
  looked carry a "new" mark. The card shows the number of commits on the
  task's branch and how many decisions Claude left for me (from
  `SUMMARY.md`).
- **Needs attention**: failed tasks, and tasks deferred more often than
  their limit allows.

A task whose prompt Claude is still writing shows in the column it will
go to, marked "drafting"; it cannot be dragged until the draft is done
(section 3, "New task form").

Dragging between columns: On hold <-> Queue; Done or Needs attention ->
Queue means "run it again" (on the same branch, with the same or an edited
prompt).

### Task card and detail panel

The card shows the title, the project (folder name), priority, model and
effort when they differ from the defaults, and attempts so far. A task that
starts after another one says so ("waits for ..." until that task is
done). A finished task's card says when its branch is already merged, and
warns when it would conflict with its base branch or with another
finished task's branch (section 6, "Tasks on the same project"). While
Claude writes the prompt the card says "drafting"; when the draft failed
it shows the error, and when Claude asked questions it shows how many and
whether one blocks the task.

Clicking a card opens a side panel:

- after a draft, Claude's questions with their assumptions and an answer
  field each; after a failed draft, the error with "Retry the draft" and
  "Write it myself";
- the prompt and all settings, editable while the task is not running;
- `SUMMARY.md` rendered, when there is one;
- the branch: name, base commit, list of commits, changed files with a
  diff view, and the merge check (which branches it conflicts with, and in
  which files);
- the runs: start, end, result, nightshift exit code, link to the log;
- the full log of the running attempt, live;
- buttons: Run next, Hold, Retry, Archive, Delete, Open worktree folder,
  Copy merge command (section 6, "After a task").

### New task form

The form asks first what I want, not for a page of fields, and does not
make me wait for Claude.

**1. What should Claude do?** The project folder and, in my own words,
what the task is, then the task's settings:

| Field | Default | Notes |
|---|---|---|
| Priority | Normal | High / Normal / Low |
| Put in | Queue | Queue or On hold: where the task goes once its prompt is written |
| Start from | the project's current branch | a branch (the task's branch starts from its latest commit), or "after" another task of the same repository (section 6, "Tasks on the same project") |
| Model, effort | from Settings | folded away; the model is chosen from a list: Opus, Sonnet, Haiku, Fable (the aliases `claude --model` takes, each the latest version; Fable notes that some plans need usage credits for it), or Other for a full model name |
| Windows per attempt | from Settings (2) | folded away; how many 5-hour windows one attempt may use (nightshift `-w`) |
| Attempts | 3 | folded away; how many times a deferred task is retried before it needs attention |

Then either **Add** or **Write it myself**. Add creates the task at once,
in the "drafting" state, and closes the form; Claude writes the prompt in
the background (below). Write it myself goes to step 2.

The project folder is the board's own picker, not the browser's
suggestion list (which cannot be styled and scrolls too fast). Typing
filters the list; it offers, in this order:

- recent project folders (from the tasks);
- project folders under the project roots: folders up to two levels down
  that hold `.git` or a build file (`pyproject.toml`, `package.json`,
  `Makefile`, ...) or `CLAUDE.md`/`AGENTS.md`. The parent folders of
  recent projects count as project roots too, so a folder used once brings
  its neighbours with it;
- when what I type starts with `/` or `~`, the subfolders of that path, so
  any folder can be reached by typing it (Tab goes into the highlighted
  folder).

Arrow keys move, Enter picks, Esc closes. Under the field the form says
whether the folder is a git repository (the task gets its own branch) or
not (it then runs in the folder itself, without a branch, see section 6),
and warns when the repository has uncommitted changes (they are not part
of the task's starting point, and merging the task's branch later can
conflict with them).

The warning offers **Commit now**, so I can deal with them while I am
there to see what gets committed. It opens the list of changed files
(from `git status --porcelain`; the first 50, then "and N more") and a
commit message field that starts empty; the commit button stays disabled
while the message is empty, so nothing is committed with a text I did not
write. The commit takes all changes (`git add -A`, which respects
`.gitignore`) on the branch the repository has checked out, with my own
git identity and with the repository's hooks running. Afterwards the form
reads the project again: the warning goes away and the task's branch
starts from the new commit. Errors are shown in the form in plain words:
a hook that failed (with its output), no git identity (with the command
to set one), nothing left to commit, or a detached HEAD (no branch to
commit on; the board does not guess). Only the project folder's own
repository is committed, never pushed, and only when I press the button;
a task itself never commits my checkout.

**2. Write the prompt** (only with Write it myself): the prompt and the
title (by default the first line of the prompt; headings that come from
the prompt template are skipped). "Back" returns to step 1 without losing
anything. The buttons stay at the bottom of the form at all times; on the
phone the form fills the screen.

With "Write it myself" the prompt field opens with the prompt template
from Settings: a few headings with one line in angle brackets under each
that says what goes there (the description from step 1, if any, replaces
the first one). The form refuses to add the task while a line from the
template is still there unchanged, so every hint is either replaced or
deleted, and while nothing but the template's headings is left. An empty
template gives an empty field.

The default template asks for what the guides on unattended coding agents
agree on (Anthropic's prompting and Claude Code best practices, the
long-running harness article, OpenAI's Codex best practices and ExecPlans,
Devin's and GitHub Copilot's task guides):

| Section | Why |
|---|---|
| Goal | what should be true, and why: Claude makes better calls when it knows the reason |
| Context | where to start, and code to follow as a pattern, so the first session is not spent searching |
| Constraints | what must not change and what is out of scope |
| Done when | observable results, each with the command that checks it: without a check Claude stops when the work only looks done |
| Milestones | for long tasks, steps that can each be checked on their own, so work across sessions stays incremental |
| When unsure | which choices Claude may make alone, which to leave for me, and what to do when something blocks the task |

Rules that hold for every task (no questions, STATE.md, small checked
steps, not weakening tests, SUMMARY.md) are in the preamble (section 6),
not in the template.

**Claude writes the prompt in the background.** After Add, the service
drafts the prompt from the description in about 10 to 20 seconds, while
the card is on the board with a "drafting" mark (in the column chosen
under "Put in"). A drafting task is never started by the runner: it has
its own status, `drafting`, and the queue only takes `queued` tasks; it
cannot be moved or have its prompt edited until the draft is done. It is
fast because the draft is one model call, not an agent session that reads
the project file by file:

1. **Project pack**, built in Python without a model: the file list
   (`git ls-files`, or a walk of the folder when it is not a repository;
   capped), the start of `README`, `CLAUDE.md` and `AGENTS.md`, the build
   and test commands (`pyproject.toml`, the `scripts` of `package.json`,
   `Makefile` targets, the `run` steps of the CI workflows in
   `.github/workflows/`), and the files the description names or that
   match its key words (a cheap search over paths and contents, the best
   matches first, capped by total size). The part that does not depend on
   the description is cached per project and reused while git `HEAD`
   stays the same; a new commit rebuilds it.
2. **One `claude -p` call** with the pack inline in the request (sent on
   standard input, since it can be larger than one command-line argument
   may be) and no tools: `--tools ""`, `--strict-mcp-config`,
   `--no-session-persistence`, `--output-format json`, `--json-schema`
   for the structured answer, and always `--model` and `--effort` from the
   two draft settings (section 3, "Settings"; default Sonnet at low
   effort), so the user's own Claude Code defaults do not decide them. An
   empty setting leaves the flag out (Claude Code's default).

The answer is the prompt, written to the template from what the pack
shows (real paths, the project's own build and test commands; where the
pack does not show something, the prompt says where to look instead of
guessing), a title, and up to five questions where my description leaves
a choice open that matters. Each question comes with its `assumption`
(what the prompt assumes, and is written into the prompt) and a
`blocking` flag. The request says that the default is to assume and go
on: `blocking` is true only when the task cannot sensibly be done without
the answer (contradictory goals, a choice between incompatible designs
where a guess wastes the whole run).

When the draft is done it fills the task's prompt and title. With a
blocking question the task goes On hold; otherwise it goes where "Put in"
said (the queue by default). If the draft fails (Claude not installed,
not signed in, usage limit, a bad answer), the task goes On hold with the
error on its card and two ways on: "Retry the draft", or "Write it
myself", which fills the prompt with the template and the description to
edit by hand. A task left drafting when the service stopped is drafted
again when it starts.

The questions are listed on the card, each with its assumption and an
answer field. Answering is optional and needs no new call to Claude: the
answered questions are added to the end of the prompt in a short
"## Answers from the user" section (each question with its answer), which
says that these answers replace what the prompt assumed; answering again
rewrites that section. Unanswered questions keep the assumption written
in the prompt. A task held for blocking questions cannot be queued until
every blocking question is answered. The prompt stays editable on the
card, like any task's.

The draft uses a little of the normal Claude usage; it is something I
start, so the queue's threshold does not apply. It calls `claude`
directly, not nightshift: it is one short call with no usage logic. Tasks
added from the command line are not drafted and do not use the template;
the `/backlog` skill follows it (`claude-backlog template` prints it).

"Duplicate" on any card fills the form from that task.

### Header

- **New task** opens the new task form (on the phone, the button at the
  bottom of the screen).
- **Weekly usage bar** with a marker at the stop threshold. Clicking the
  threshold changes it (the same setting as in Settings).
- **Status line**: idle / running (started by me or because I was away) /
  stopped and why.
- **Starts when away: ON / OFF**, one click to switch. OFF is for days
  when I want to keep the week's budget for myself.
- **Run now**, with a small menu: "until the queue is empty or the limit
  is reached" (default) or "until HH:MM" (no new task starts after that
  time). This is the "I am leaving" button.
- **Stop after this task** and, in its menu, **Stop now** (the running task
  goes back to the queue; its `STATE.md` and its branch keep its progress).

### Settings

- Stop threshold, weekly usage % (default 50).
- Start when away: on/off (default on), and after how many minutes of
  screensaver (default 15).
- When I come back to a run that started by itself: stop now (default) or
  let the current task finish.
- Defaults for model (the same list as in the new task form), effort,
  windows per attempt, attempts.
- Writing prompts with Claude: the model (Sonnet, Haiku, Opus, or Claude
  Code's default; setting `draft_model`, default `sonnet`, the alias, not
  a pinned model) and the effort (setting `draft_effort`, default `low`;
  empty means Claude Code's default). They are separate from the task
  model, which is for runs only.
- Project roots for the folder picker (default: the home folder; the
  parent folders of recent projects are added by themselves).
- Prompt template for the new task form (section 3, "New task form"),
  with a button that restores the default one.
- Port (default 8765; the Tailscale port is the next one).
- Phone access: my Tailscale login, the phone address and its QR code.

### On the phone

The same page, laid out for a narrow screen: the columns become tabs
(Queue first), and every card has large "to top", "up", "down" and "hold"
buttons, because dragging on a phone is fiddly. Everything else works as
on the computer: Run now, Stop, Starts when away ON/OFF, adding a task,
reading `SUMMARY.md`, the live log. A change made on the phone shows up at
once on the computer and the other way round (Server-Sent Events).

Typical use when I am out: open the board, move the task I want next to
the top of the queue (or press "Run next" on it), and, if nothing is
running, press Run now.

### Access

Tasks run with `--dangerously-skip-permissions`, so whoever can reach the
board can run any command as me. The board is therefore never open to the
internet or the local network.

- **On the computer**: the service listens on `127.0.0.1:8765`, no login.
  Every request that changes something must come from the board's own
  origin (Origin header check), so other web pages open in the browser
  cannot use it.
- **From the phone**: through [Tailscale](https://tailscale.com), a private
  network between my own devices. Tailscale is already installed on this
  machine (stopped at the moment); the phone needs the Tailscale app,
  logged in to the same account. Then
  `tailscale serve --bg --https=443 http://127.0.0.1:8766` makes the board
  reachable from my devices only, at
  `https://<machine>.<tailnet>.ts.net`, with a real HTTPS certificate.
  It works from anywhere (mobile data, other Wi-Fi) without opening any
  port on the router.
  - Tailscale Funnel (public internet) is never used.
  - The service listens on a second port, `127.0.0.1:8766`, only for
    Tailscale. There every request must carry the `Tailscale-User-Login`
    header that `tailscale serve` adds, and it must match my login (set in
    Settings). So even a device on the tailnet that is not mine gets
    nothing.
  - `claude-backlog install --phone` runs the `tailscale serve` command
    once; it persists across reboots. The board's Settings show the phone
    address as a QR code to open it on the phone the first time and add it
    to the home screen.
  - When Tailscale is not running, phone access is simply off; the board
    on the computer is not affected.

## 4. Running the queue

The queue starts in two ways:

1. **Run now** on the board, or `claude-backlog go`.
2. **When I am away** (if "Starts when away" is ON): the screensaver has
   been on for N minutes (default 15) and the queue has a task that can
   run (one that does not wait for another task).

It runs one task after another and stops when:

- nightshift reports that the weekly threshold is reached (exit 3);
- the queue is empty, or the tasks left in it all wait for other tasks;
- the "until HH:MM" time has passed (the running task is not cut short;
  its windows-per-attempt limit bounds it);
- I press Stop after this task, or Stop now;
- I come back, if the run started because I was away (below).

### Away and back

"Away" means the screensaver has been on for N minutes. The screensaver
is a better signal than the lock: many people have it come on without
locking (the author's machine, Cinnamon on X11, does exactly that), and
video players keep it off, so watching something does not count as away.

- **Away**: every 30 seconds the watcher asks the desktop how long the
  screensaver has been on, over the session D-Bus. One small detector per
  desktop, tried in this order:

  | Desktop | Source |
  |---|---|
  | Cinnamon | `org.cinnamon.ScreenSaver.GetActiveTime` |
  | GNOME | `org.gnome.ScreenSaver.GetActiveTime` |
  | KDE Plasma, Xfce, others | `org.freedesktop.ScreenSaver.GetActiveTime` |
  | anything with logind | `LockedHint` / `IdleHint` of the session (no duration: away is counted from when the hint was first seen) |

  When the time reaches N minutes, the queue starts. With a screensaver
  that comes on after 5 minutes and the default N of 15, that is about 20
  minutes after the last key press. The board shows which detector is in
  use and its current reading, so a user can see at once whether away
  detection works on their desktop. New detectors are a small,
  self-contained contribution.
- **Back**: the screensaver goes off. For a run that started because I was
  away, the runner then stops the running task right away (Stop now: it
  goes back to the queue, and its progress is kept in `STATE.md` and in a
  work-in-progress commit on its branch), so Claude does not use the same
  5-hour window as I do. A setting can make it finish the current task
  instead. A run I started with Run now is not stopped when I come back.
- If I leave again, the next "away" starts the queue again, and the
  stopped task continues from where it was.
- When the weekly threshold has been reached, each "away" still starts the
  queue, nightshift sees the threshold and exits 3 at once, without
  starting Claude. Nothing is spent; the board and the notification say
  "limit reached" only once per week.

Things that would stop it from working, and what to do:

- **Idle suspend**: if the machine suspends after less idle time than the
  away delay, the queue never starts. Once the queue runs, the inhibitor
  (below) keeps the machine awake. Where the desktop exposes its power
  settings (GNOME and Cinnamon through gsettings), the board reads them and
  warns when the suspend timeout is shorter than the away delay; otherwise
  the README explains what to check.
- **Closing the lid** suspends the laptop even with the inhibitor, because
  logind lets the lid switch override it. Leave the lid open.

While the queue runs, `systemd-inhibit --what=sleep:idle` keeps the machine
awake. The lock covers the whole run, including nightshift's waits for the
5-hour reset, and is released when the queue stops, so the machine can
sleep afterwards.

### The weekly limit

One setting, "stop at X% of the weekly limit" (default 50), is passed to
nightshift as `-t X` for every task. A task may override it. nightshift does
not start a new session once usage is at or above X, so the remaining
(100 - X)% is left for my own use.

## 5. Data

SQLite, so the board and the runner can both write safely, and order can
be changed by dragging without rewriting files.

**tasks**

| Column | Meaning |
|---|---|
| `id` | short slug from the title plus a number, e.g. `api-v2-17`; also used for the branch and folder names |
| `title`, `prompt` | |
| `project_dir` | absolute path of the project as I chose it |
| `repo_root`, `base_branch`, `base_commit` | empty when the project is not a git repository; `base_commit` is fixed at the first attempt |
| `after_task` | the task this one starts after, or empty (section 6, "Tasks on the same project"); fixed at the first attempt |
| `base_ref` | the branch `base_commit` was taken from when it is not the base branch: the branch of the task this one starts after; empty otherwise |
| `branch`, `worktree` | `backlog/<id>` and `~/.local/share/claude-backlog/worktrees/<id>`, once created |
| `status` | `drafting`, `hold`, `queued`, `running`, `done`, `attention`, `archived` |
| `description` | what I asked for in my own words, when Claude wrote the prompt from it |
| `draft_target` | where a drafting task goes when its prompt is written: `queued` or `hold` |
| `draft_error` | why the last draft failed, or empty |
| `questions` | Claude's questions from the draft (JSON: question, assumption, blocking, answer) |
| `priority` | 1 high, 2 normal, 3 low |
| `position` | order within the priority group (a float, so a drop between two cards only changes the dropped one) |
| `model`, `effort`, `max_windows`, `threshold`, `max_attempts` | settings; empty means "use the default" |
| `attempts` | attempts counted so far |
| `created_at`, `updated_at`, `finished_at`, `seen_at` | `seen_at` drives the "new" mark |

**runs**: one row per attempt: `task_id`, `started_at`, `ended_at`,
`exit_code`, `result`, `log_path`, `trigger` (`run-now` or `away`).

**settings**: key/value.

Queue order: `status = queued`, then `priority`, then `position`. Tasks
that wait for another task are skipped until that task is done. A
deferred task keeps its place, so an unfinished high-priority task is picked
up again first next time. Within one run, every queued task gets a turn
before any task gets a second one; otherwise a task that used up its
windows per attempt would start again at once and the others would wait.
When all have had a turn, a new round begins.

### Task files (import and export)

A task file format serves as an exchange format, for writing a long prompt
in an editor or keeping one in a project:

```markdown
---
title: Migrate the API client to v2
project: ~/projects/acme-app
priority: high
model: opus
effort: high
max-windows: 2
after: api-client-tests-16
---
Migrate every call in src/api/ from the v1 client to the v2 client. ...
```

`claude-backlog add task.md` imports it; `claude-backlog export ID` writes
one. The database is the only source of truth; files are not watched.

## 6. Running a task

1. Take the first queued task that does not wait for another task, mark
   it `running`, add a `runs` row.
2. Prepare where it runs:
   - **Git repository, first attempt**: record the base commit (the latest
     commit of the base branch, or of the branch of the task it starts
     after, see below) and run
     `git -C <repo_root> worktree add -b backlog/<id> ~/.local/share/claude-backlog/worktrees/<id> <base_commit>`.
     If the project folder is a subfolder of the repository, Claude works
     in the same subfolder of the worktree.
   - **Git repository, later attempt**: reuse the existing worktree and
     branch, so the work continues where it stopped.
   - **Not a git repository**: run in the project folder itself, with no
     branch and no commits. The board marks such tasks.
3. Create `tasks/<id>/`. Build the prompt: the unattended preamble (below)
   plus the task's prompt. Save it to `tasks/<id>/prompt.md`.
4. Run, with the worktree (or project folder) as the working directory:

   ```
   claude-nightshift -f ~/.local/share/claude-backlog/tasks/<id>/prompt.md \
     -t <threshold> -w <max-windows> [-m <model>] [-e <effort>] \
     --status-file ~/.local/share/claude-backlog/tasks/<id>/nightshift-status.md \
     --log-file ~/.local/share/claude-backlog/tasks/<id>/logs/<run>.log
   ```

   (The whole run is already under `systemd-inhibit`, section 4.)

5. Map the exit code:

| nightshift exit | Meaning | Task becomes | Queue |
|---|---|---|---|
| `0` and `SUMMARY.md` exists | completed | done | next task |
| `0`, no `SUMMARY.md` | ended without finishing the convention | attention | next task |
| `1` | failure: Claude error, retries used up | attention | next task |
| `2` | bad arguments | attention | next task |
| `3` | weekly threshold reached | queued (deferred, keeps its place) | **stop** |
| `4` | windows for this attempt used up, unfinished | queued (attempt +1) | next task |
| `5` | usage unreadable (new in nightshift) | queued (not counted) | **stop** |
| `130` | stopped by me, or because I came back | queued (not counted) | **stop** |
| other | killed or crashed | attention | next task |

A task that reaches `max_attempts` goes to `attention` instead of back to
the queue. A task found `running` when the service starts means the
service died; it goes back to the queue and the report says so.

"Stop now" sends SIGINT to nightshift, which passes it on to Claude and
exits 130.

After the attempt, backlog commits anything Claude left uncommitted in the
worktree as one commit "backlog: work in progress (attempt N)", so nothing
is lost and the branch always shows the full state.

### Tasks on the same project

Tasks run one at a time, but each starts from the base branch as it is
when the task starts. Two tasks on the same project therefore do not see
each other's work unless the first one has been merged by then, and their
branches can conflict when both are merged. Three rules handle that:

1. **Independent by default.** Each task's branch starts from its base
   branch, so every branch can be reviewed, merged or thrown away on its
   own. Tasks that touch different parts of a project merge cleanly.
2. **"Start after" for tasks that build on each other.** A task can be set
   to start after another task of the same repository (the form's "Start
   from" list, the edit tab, `add --after ID`, `after:` in a task file).
   It stays in the queue, marked "waits for ...", and the runner skips it
   until that task is done (or archived after it was done). Its first
   attempt then starts from the other task's branch, so its own branch
   contains both; merging it brings in both. If that branch is already
   merged into the base branch, or was deleted, it starts from the base
   branch as usual. Deleting a task clears "start after" on the tasks that
   waited for it. Chains are allowed; a task cannot wait for itself, for a
   task of another repository, or in a circle.
3. **Merge check, with no model.** For every finished task whose branch is
   not merged yet, the board checks whether merging it would conflict with
   its base branch, and with the branches of the other finished tasks of
   the same repository: `git merge-tree --write-tree` (git 2.38 or later)
   merges in memory and touches no files. Results are cached by the
   commits involved, so they are only computed again when a branch moves.
   The card warns ("conflicts with main", "conflicts with <task>") and the
   detail panel lists the files. With an older git, the check is left out.
   A conflict is resolved by merging one branch, then the other by hand,
   or with a new task that merges it and resolves the conflicts.

### After a task

Backlog never merges, pushes or deletes branches on its own. For a done
task the board offers:

- the diff between the base commit and the branch, in the detail panel;
- Open worktree folder (to try the result, or open it in an editor);
- Copy merge command: `git -C <repo_root> merge backlog/<id>`;
- Archive, which removes the worktree (`git worktree remove`) but keeps
  the branch; a second button deletes the branch too, after a confirmation.

### The unattended preamble

Prepended to every prompt. `{...}` is filled in from the task; the git
lines are left out when the task does not run in a worktree, and the line
about `{base_ref}` is there only when the branch started from the branch
of the task it starts after (`{base_ref}` is otherwise the base branch).

```
This is an unattended run. Nobody will answer questions or approve anything.

- Do not ask questions and do not wait for input. When something is unclear,
  make a reasonable decision yourself (following the task's "When unsure"
  section, if it has one) and write it down in {task-dir}/STATE.md with the
  reason.
- Read {task-dir}/STATE.md first if it exists. It records what earlier
  sessions did. Trust it and continue from where it says.
- The work may take several sessions: your context is compacted when it
  fills up, and the task is resumed after usage limits. Do not stop early
  or cut corners to finish within one session.
- Keep {task-dir}/STATE.md current as you work. If you hit a session or
  usage limit, or are about to, first record the exact progress there: what
  is done, what is half done, and the next step, so the work can be resumed.
- You are working in a separate git worktree on the branch {branch}, started
  from {base_ref} at {base_commit}. It is a fresh checkout: install
  dependencies if the task needs them, and expect files that are not in git
  (for example .env) to be missing.
- Work in small steps. After each step, run the relevant checks and commit
  on {branch} with a clear message, so every commit leaves the project
  working. Do not push, do not switch or create branches, do not merge.
- Only change files inside this worktree and {task-dir}. Do not touch
  anything else, including the original project folder {project_dir}.
- {base_ref} is the branch of the task "{after_title}", which ran before
  this one and is not merged into {base_branch} yet. Its work is already in
  this worktree: build on it, and do not undo it.
- Do only what the task asks: no features, refactoring or configurability
  it does not ask for.
- Fix causes, not symptoms. Do not delete, skip or weaken tests or checks
  to make them pass, do not hard-code results for test inputs, and do not
  bypass git hooks. If a test or the task itself looks wrong, say so in
  SUMMARY.md instead of working around it.
- If something blocks part of the task (a missing credential, a service
  that is down), do the rest, and record what is blocked and why.
- Remove temporary files and scripts you made for yourself.
- Do not end your turn while any agent you started is still running. Wait
  for all of them to finish.
- Before you finish, check every completion criterion of the task (its
  "Done when" section, if it has one) and run the checks one last time.
- When the task is done, write {task-dir}/SUMMARY.md: what was done, the
  checks you ran and their results, problems left open, and a section
  "Decisions for me". Mark everything done in STATE.md. Write SUMMARY.md
  only when the task is really finished; it is the signal that the task is
  complete.

The task:
```

- `STATE.md` carries progress across attempts; nightshift's status file
  carries it across 5-hour windows within one attempt.
- The "Decisions for me" section is what the Done card counts.

## 7. Adding tasks

Writing a prompt that works without me is the hard part, so there are
three ways, from most to least help:

1. **`/backlog` in a Claude Code session.** When I am working with Claude
   and see a long job, I type `/backlog` (optionally with a hint). Claude
   writes a self-contained unattended prompt from the conversation and the
   project (goal, files, how to check the result, what not to touch), shows
   it with the proposed title, priority and folder, and on my OK runs
   `claude-backlog add`. When the task builds on another task of the same
   project that is not merged yet, it adds `--after <id>`. Before the
   proposal it runs `git -C <project folder> status --porcelain`; when the
   repository has uncommitted changes, the proposal says so and offers to
   commit them first, with the list of files and a proposed message. It
   commits only after my OK, and never as part of the task's prompt. The
   skill ships with the tool and is installed by `claude-backlog install`.
2. **The board's form** (section 3): a description in my own words, and
   Claude writes the prompt in the background.
3. **CLI**: `claude-backlog add -C ~/projects/acme-app -p high "prompt"`,
   `add task.md`, or `add -` to read the prompt from stdin. When the
   project's repository has uncommitted changes, `add` prints a one-line
   note on stderr (they are not part of the task's starting point); the
   task is still added and the exit code does not change.

## 8. Results

- The Done column with "new" marks, and the detail panel with
  `SUMMARY.md` and the branch diff, are the "when I am back" view.
- Each run of the queue also writes `reports/YYYY-MM-DD-HHMMSS.md`: why it
  stopped, a table of tasks (result, exit code, time, commits, log), and
  for each finished task the first lines of its `SUMMARY.md`. The board
  links it.
- A desktop notification (`notify-send`) when the queue stops.

## 9. CLI

```
claude-backlog serve                 run the service in the foreground (what the unit runs)
claude-backlog install [--phone] | uninstall
                                     systemd user unit, /backlog skill, desktop launcher;
                                     --phone also sets up tailscale serve
claude-backlog open                  open the board in the browser
claude-backlog add [-C DIR] [-p high|normal|low] [--hold] [--after ID] PROMPT | FILE | -
claude-backlog list                  the queue in run order, plus running and attention
claude-backlog go [--until HH:MM]    same as Run now
claude-backlog stop [--now]          stop after the current task, or now
claude-backlog export ID             write the task as a task file
claude-backlog report                print the latest run report
claude-backlog template              print the prompt template (the /backlog skill follows it)
```

The CLI talks to the service's API when it is running and to the database
directly when it is not (except `go` and `stop`, which need the service).

## 10. Stack

- Python 3.10+, uv, same layout and test style as nightshift.
  claude-nightshift is a normal package dependency, so installing backlog
  installs it too.
- Starlette + uvicorn for the web server, Jinja2 for the page, sqlite3 from
  the standard library.
- Front end: one HTML page with plain JavaScript, no build step.
  SortableJS (vendored, so it works offline) for drag and drop. Live
  updates through Server-Sent Events.
- Tests: the runner with a fake nightshift script that exits with each code
  and a throwaway git repository; API tests; the presence watcher with
  fake D-Bus answers. No test needs Claude, a Claude login, a desktop or
  systemd, so they run in CI.

## 11. Phases

**Phase 1** is the first version that can really be used: everything below,
built in this order.

1. Database, task model, CLI `add` / `list` / `export`.
2. Runner: worktree and branch, prompt builder, task folder, nightshift
   call, exit code mapping, attempts, recovery, `systemd-inhibit`.
3. nightshift: `--usage-json`, `--status-file`.
4. Service: API, Run now, Stop, presence watcher (away and back).
5. Board: columns, drag and drop, new task form (with the prompt drafted
   in the background from a project pack), detail panel with diff and
   live log, usage bar, settings, phone layout.
6. Phone access: second port with the Tailscale login check,
   `install --phone`, QR code.
7. Report file and desktop notification.
8. `install`: systemd unit, `/backlog` skill, launcher.
9. One real run with several tasks while I am away, to check the away
   start, the stop when I come back, deferral, worktrees and inhibit.

**Phase 2** (later, only if wanted): notifications on the phone (for
example through ntfy) when the queue stops or a task needs attention, a
"check this prompt" button, a merge button on the board.

## 12. Open source

The project is written from the start to be published on GitHub and used
by others.

- **License**: MIT, like nightshift.
- **Install**: `uv tool install git+https://github.com/SciScend/claude-backlog`
  (or `pipx`), then `claude-backlog install`. A PyPI release can follow
  once nightshift is on PyPI too.
- **Requirements**, stated in the README: Linux with systemd, Claude Code
  installed and logged in (the user's own subscription), git, Python 3.10+.
  Tailscale only for phone access.
- **Nothing personal in the repository**: no paths, names or settings from
  the author's machine in code, defaults, tests or examples. Everything
  machine-specific is a setting with a neutral default. The hand-made run
  that led to this design (the author's scripts, with the author's paths)
  is kept out of the repository.
- **Safety first in the README**: a short, visible section on what
  `--dangerously-skip-permissions` means, why tasks run in a separate
  worktree, why the board listens only on localhost, and why phone access
  goes through Tailscale and never through Funnel or an open port.
- **English** for the UI, docs, code and commit messages. UI texts are
  kept in one place so translations can be added.
- **No telemetry.** The tool talks only to Claude Code (through
  nightshift) and to the local machine.
- **Repository files**: README with a screenshot and a short GIF of the
  board, CHANGELOG, CONTRIBUTING (how to run the tests, how to add a
  presence detector), SECURITY (how to report a problem), issue templates.
- **CI**: GitHub Actions running pytest and ruff on Python 3.10-3.13.
- **Versioning**: semantic versions; the database has a schema version and
  migrations from the first release on, so updates never lose a backlog.

## 13. Out of scope

- Starting the queue at set times (timers, run windows).
- Running tasks in parallel, or on more than one machine.
- Usage or budget logic of its own (nightshift's job).
- Dependencies between tasks beyond "start after one other task"
  (section 6, "Tasks on the same project").
- Pushing, opening pull requests, merging automatically.
- macOS and Windows in the first version. The platform-specific parts
  (service, keeping awake, away detection) sit behind three small
  interfaces, so a macOS port (launchd, `caffeinate`, `ioreg` idle time)
  can be added later, also by contributors.

## 14. Decisions and open questions

Decided in review (2026-10-18):

1. **Exit 0 without `SUMMARY.md`**: the task needs attention, so a person
   looks at it. No automatic retry.
2. **Unreadable usage**: nightshift gets its own exit code (`5`), and
   backlog stops the queue on it.
3. **Windows per attempt**: default 2, so one big task cannot take the
   whole run while others wait; it can be raised per task.
4. **GitHub owner**: the SciScend organization, next to claude-nightshift.
   The git identity for this folder follows from that.
5. **Name**: `claude-backlog`. It says plainly what the tool is. The
   README says it is unofficial and not affiliated with Anthropic.
6. **Tasks on the same project**: independent branches by default, "start
   after" for tasks that build on each other, and a merge check on the
   board; backlog still never merges on its own (section 6).
