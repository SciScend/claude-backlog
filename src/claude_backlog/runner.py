"""The runner: works through the queue, one or more tasks at a time, through nightshift."""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_backlog import db, gitops, paths, report, settings, tasks
from claude_backlog.prompt import build_prompt
from claude_backlog.system import Inhibitor, notify
from claude_backlog.usage import nightshift_command, read_runs

__all__ = ["Runner", "Attempt", "recover", "map_exit", "spend_args"]

logger = logging.getLogger(__name__)

# nightshift exit codes
COMPLETED, FAILED, BAD_ARGS, WEEKLY_LIMIT, RUN_BUDGET, USAGE_UNKNOWN = 0, 1, 2, 3, 4, 5
INTERRUPTED = 130
KNOWN_EXITS = {COMPLETED, FAILED, BAD_ARGS, WEEKLY_LIMIT, RUN_BUDGET, USAGE_UNKNOWN,
               INTERRUPTED}

# How long nightshift gets to wind down after SIGINT before it is killed.
STOP_GRACE_SECONDS = 30


@dataclass
class Attempt:
    """What one attempt came to."""

    task_id: str
    title: str
    exit_code: int | None
    result: str  # done, deferred, attention, interrupted, limit, usage-unknown
    status: str  # the task's status afterwards
    counted: bool  # whether it used up one of the task's attempts
    stop_queue: bool
    note: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    log_path: str = ""
    commits: int = 0
    spent: float | None = None  # weekly percent, from nightshift's ledger


@dataclass
class Mapping:
    result: str
    status: str
    counted: bool
    stop_queue: bool
    note: str = ""
    extra: dict = field(default_factory=dict)


def map_exit(code, summary_exists, attempts_after, max_attempts) -> Mapping:
    """Decide what an attempt's nightshift exit code means for the task and queue.

    ``attempts_after`` is the attempt count if this attempt is counted.
    """
    if code == COMPLETED and summary_exists:
        return Mapping("done", "done", True, False)
    if code == COMPLETED:
        return Mapping("attention", "attention", True, False,
                       "Claude finished without writing SUMMARY.md")
    if code == FAILED:
        return Mapping("attention", "attention", True, False,
                       "nightshift failed (exit 1): see the log")
    if code == BAD_ARGS:
        return Mapping("attention", "attention", True, False,
                       "nightshift rejected its arguments (exit 2): see the log")
    if code == WEEKLY_LIMIT:
        return Mapping("limit", "queued", False, True,
                       "weekly limit reached")
    if code == RUN_BUDGET:
        if attempts_after >= max_attempts:
            return Mapping("attention", "attention", True, False,
                           f"not finished after {attempts_after} attempt(s)")
        return Mapping("deferred", "queued", True, False)
    if code == USAGE_UNKNOWN:
        return Mapping("usage-unknown", "queued", False, True,
                       "weekly usage could not be read")
    if code == INTERRUPTED:
        return Mapping("interrupted", "queued", False, True, "stopped")
    return Mapping("attention", "attention", True, False,
                   f"nightshift ended unexpectedly (exit {code}): see the log")


def _inside(a, b) -> bool:
    """Is folder ``a`` the folder ``b`` or inside it?"""
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def _clash(task, other) -> bool:
    """May ``task`` not run while ``other`` does? The same task, or folders
    outside git that overlap: those have no worktrees to keep them apart."""
    if task["id"] == other["id"]:
        return True
    if task["repo_root"] or other["repo_root"]:
        return False
    a, b = task["project_dir"], other["project_dir"]
    return _inside(a, b) or _inside(b, a)


def recover(conn) -> list[str]:
    """Put back tasks left running when the service died. Returns their ids."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM tasks WHERE status = 'running'")]
    now = db.now()
    with db.transaction(conn):
        for task_id in ids:
            conn.execute(
                "UPDATE tasks SET status = 'queued', note = ?, updated_at = ? WHERE id = ?",
                ("the service stopped while this task was running", now, task_id),
            )
        conn.execute(
            "UPDATE runs SET ended_at = ?, result = 'interrupted' WHERE ended_at IS NULL",
            (now,),
        )
    return ids


class Runner:
    """Runs the queue in a background thread. One instance per service.

    Up to the "parallel" setting, tasks run at the same time, each in a
    worker thread of its own with its own nightshift.
    """

    def __init__(self, db_path=None, inhibitor=None, notifier=notify, on_change=None):
        self.db_path = db_path
        self.inhibitor = inhibitor or Inhibitor()
        self.notifier = notifier
        self.on_change = on_change or (lambda: None)
        self._lock = threading.RLock()
        self._thread = None
        self._finished = queue.Queue()  # attempts handed back by the workers
        self._repo_locks = {}
        self.slots = {}  # task id -> {"proc", "started_at", "run_key"}
        self.state = "idle"  # idle, running, stopping
        self.trigger = None  # run-now, away
        self.until = None
        self.started_at = None
        self.stop_mode = None  # None, after, now
        self.stop_cause = None  # "you", "back", "service"
        self.stop_requested_at = None
        self.last_stop = None  # {"reason", "at", "trigger"}
        self.version = 0

    # --- control ----------------------------------------------------------

    def _changed(self) -> None:
        self.version += 1
        self.on_change()

    def start(self, trigger="run-now", until=None) -> bool:
        """Start working through the queue. False if it is already running."""
        with self._lock:
            if self.state != "idle":
                if trigger == "run-now" and self.trigger == "away":
                    # I pressed Run now while an away run was going: it is mine now,
                    # so coming back no longer stops it.
                    self.trigger = "run-now"
                    self.until = until
                    self._changed()
                    return True
                return False
            self.state = "running"
            self.trigger = trigger
            self.until = until
            self.started_at = time.time()
            self.stop_mode = None
            self.stop_cause = None
            self.stop_requested_at = None
            self._finished = queue.Queue()
            self._thread = threading.Thread(target=self._loop, name="backlog-runner",
                                            daemon=True)
            self._thread.start()
            self._changed()
            return True

    def stop(self, now=False, cause="you") -> bool:
        with self._lock:
            if self.state == "idle":
                return False
            self.stop_mode = "now" if now else (self.stop_mode or "after")
            self.stop_cause = cause
            self.state = "stopping"
            if now:
                self.stop_requested_at = time.time()
                for slot in self.slots.values():
                    self._interrupt(slot.get("proc"))
            self._changed()
            return True

    @staticmethod
    def _interrupt(proc) -> None:
        if proc and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(proc.pid, signal.SIGINT)

    def shutdown(self, timeout=STOP_GRACE_SECONDS) -> None:
        """Stop now and wait for the thread (service shutdown)."""
        self.stop(now=True, cause="service")
        thread = self._thread
        if thread:
            thread.join(timeout)

    def wait(self, timeout=None) -> None:
        thread = self._thread
        if thread:
            thread.join(timeout)

    def running(self) -> list[dict]:
        """The tasks running now: task id, start time and nightshift run id."""
        with self._lock:
            return [{"task_id": task_id, "started_at": slot["started_at"],
                     "run_key": slot.get("run_key")}
                    for task_id, slot in self.slots.items()]

    def status(self) -> dict:
        with self._lock:
            running = self.running()
            first = running[0] if running else {}
            return {
                "state": self.state,
                "trigger": self.trigger,
                "until": self.until,
                "started_at": self.started_at,
                "tasks": running,
                # The first running task, for clients that show only one.
                "task_id": first.get("task_id"),
                "task_started_at": first.get("started_at"),
                "stop_mode": self.stop_mode,
                "last_stop": self.last_stop,
                "inhibit": self.inhibitor.available,
            }

    # --- the loop ---------------------------------------------------------

    def _halt_reason(self):
        """Why no new task may start now, or None."""
        if self.stop_mode:
            return "you came back" if self.stop_cause == "back" else "stopped by you"
        if self.until and time.time() >= self.until:
            return "the 'until' time has passed"
        return None

    def _loop(self) -> None:
        conn = db.connect(self.db_path)
        attempts = []
        tried = set()
        halt = None  # why no new task starts any more
        started = time.time()
        trigger = self.trigger
        try:
            self.inhibitor.start("working through the claude-backlog queue")
            while True:
                halt = halt or self._halt_reason()
                if halt is None:
                    self._fill(conn, tried)
                    if not self.slots:
                        halt = ("the queue is empty" if not tasks.queue(conn) else
                                "the tasks left in the queue wait for other tasks")
                if halt and not self.slots:
                    break
                try:
                    attempt = self._finished.get(timeout=0.5)
                except queue.Empty:
                    continue
                attempts.append(attempt)
                with self._lock:
                    self.slots.pop(attempt.task_id, None)
                    self._changed()
                if attempt.stop_queue and not halt:
                    if attempt.result == "interrupted":
                        halt = self._halt_reason() or "stopped by you"
                    else:
                        halt = attempt.note
        except Exception as exc:  # the service must survive anything a run does
            logger.exception("the runner failed")
            halt = f"internal error: {exc}"
            self.stop(now=True, cause=self.stop_cause or "you")
            self._drain(attempts)
        finally:
            self._finish(conn, attempts, started, halt, trigger)

    def _drain(self, attempts, timeout=STOP_GRACE_SECONDS + 10) -> None:
        """Wait for the workers still running after the loop failed."""
        deadline = time.time() + timeout
        while self.slots and time.time() < deadline:
            with contextlib.suppress(queue.Empty):
                attempt = self._finished.get(timeout=0.5)
                attempts.append(attempt)
                with self._lock:
                    self.slots.pop(attempt.task_id, None)

    def _finish(self, conn, attempts, started, reason, trigger) -> None:
        self.inhibitor.stop()
        ended = time.time()
        quiet = (not attempts and self.last_stop
                 and self.last_stop.get("reason") == reason)
        try:
            if attempts:
                report.write(conn, started, ended, reason, trigger, attempts)
            if not quiet and self.stop_cause != "service":
                self.notifier("claude-backlog stopped", report.summary_line(reason,
                                                                           attempts))
        except Exception:
            logger.exception("could not write the report")
        conn.close()
        with self._lock:
            self.state = "idle"
            self.slots = {}
            self.last_stop = {"reason": reason, "at": ended, "trigger": trigger,
                              "tasks": len(attempts)}
            self._changed()

    def _fill(self, conn, tried) -> None:
        """Start tasks until every parallel slot is taken or none can start."""
        parallel = settings.get(conn, "parallel")
        while len(self.slots) < parallel:
            busy = []
            for task_id in list(self.slots):
                with contextlib.suppress(tasks.TaskError):
                    busy.append(tasks.get(conn, task_id))
            task = self._pick(conn, tried, busy)
            if task is None:
                return
            tried.add(task["id"])
            with self._lock:
                self.slots[task["id"]] = {"proc": None, "started_at": time.time(),
                                          "run_key": None}
                self._changed()
            threading.Thread(target=self._work, args=(task, self._finished),
                             name=f"backlog-{task['id']}", daemon=True).start()

    def _work(self, task, finished) -> None:
        """One attempt, in a worker thread with its own database connection."""
        conn = db.connect(self.db_path)
        try:
            attempt = self._run_task(conn, task)
        except Exception as exc:
            logger.exception("the attempt at %s failed", task["id"])
            note = f"internal error: {exc}"
            with contextlib.suppress(Exception):
                tasks.set_status(conn, task["id"], "attention", note=note,
                                 finished_at=time.time())
            attempt = Attempt(task["id"], task["title"], None, "attention", "attention",
                              False, False, note, time.time(), time.time())
        finally:
            conn.close()
        finished.put(attempt)

    @staticmethod
    def _pick(conn, tried, busy=()):
        """The next task: each queued task gets a turn before any gets a second one.

        A task that used up its windows goes back to its place in the queue, so
        without this it would run again at once and other tasks would wait.
        When every queued task has had its turn, a new round begins. Tasks
        that are running (``busy``), and tasks in a folder outside git that
        a running task works in, are left out.
        """
        queued = [t for t in tasks.runnable(conn)
                  if not any(_clash(t, other) for other in busy)]
        if not queued:
            return None
        for task in queued:
            if task["id"] not in tried:
                return task
        tried.clear()
        return queued[0]

    def _repo_lock(self, repo_root):
        """Serializes git commands on one repository between the workers."""
        with self._lock:
            return self._repo_locks.setdefault(repo_root, threading.Lock())

    def _run_task(self, conn, task) -> Attempt:
        conf = settings.get_all(conn)
        task_id = task["id"]
        tdir = paths.task_dir(task_id)
        (tdir / "logs").mkdir(parents=True, exist_ok=True)
        summary = tdir / "SUMMARY.md"
        if task["attempts"] == 0 and summary.exists():
            # Left from an earlier round; this round must write its own.
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            summary.rename(tdir / f"SUMMARY-{stamp}.md")

        started = time.time()
        try:
            with self._repo_lock(task["repo_root"]):
                cwd = self._prepare(conn, task)
        except (gitops.GitError, OSError) as exc:
            note = f"could not prepare the git worktree: {exc}"
            tasks.set_status(conn, task_id, "attention", note=note, finished_at=started)
            return Attempt(task_id, task["title"], None, "attention", "attention", False,
                           False, note, started, time.time())
        task = tasks.get(conn, task_id)

        prompt_file = tdir / "prompt.md"
        after_title = None
        if task["base_ref"] and task["after_task"]:
            with contextlib.suppress(tasks.TaskError):
                after_title = tasks.get(conn, task["after_task"])["title"]
        prompt_file.write_text(build_prompt(task, tdir, after_title), encoding="utf-8")

        with db.transaction(conn):
            cur = conn.execute(
                "INSERT INTO runs (task_id, started_at, trigger) VALUES (?, ?, ?)",
                (task_id, started, self.trigger),
            )
            run_id = cur.lastrowid
            log_path = tdir / "logs" / f"{run_id}.log"
            conn.execute("UPDATE runs SET log_path = ? WHERE id = ?",
                         (str(log_path), run_id))
            conn.execute(
                "UPDATE tasks SET status = 'running', note = NULL, updated_at = ? "
                "WHERE id = ?", (started, task_id),
            )

        run_key = f"{task_id}-{run_id}"
        cmd = [*nightshift_command(), "-f", str(prompt_file),
               "-t", f"{task['threshold'] or conf['threshold']:g}",
               "--status-file", str(tdir / "nightshift-status.md"),
               "--log-file", str(log_path), "--run-id", run_key]
        windows = task["max_windows"] or conf["max_windows"]
        if windows:
            cmd += ["-w", str(windows)]
        model = task["model"] or conf["model"]
        if model:
            cmd += ["-m", model]
        effort = task["effort"] or conf["effort"]
        if effort:
            cmd += ["-e", effort]
        cmd += spend_args(task["spend"] or conf["spend"], conf["parallel"])

        env = dict(os.environ)
        if task["worktree"]:
            env.update(gitops.identity_env(cwd))
        with self._lock:
            self.slots[task_id].update(started_at=started, run_key=run_key)
            self._changed()
        console = tdir / "logs" / f"{run_id}.console.log"
        code = self._run_nightshift(task_id, cmd, cwd, env, console)
        ended = time.time()
        if code in KNOWN_EXITS:
            # nightshift logged everything to the log file as well; the console
            # copy only matters when it crashed.
            console.unlink(missing_ok=True)

        commit_count = None
        if task["worktree"]:
            try:
                with self._repo_lock(task["repo_root"]):
                    if gitops.commit_all(task["worktree"], f"backlog: commit what "
                                         f"attempt {task['attempts'] + 1} left "
                                         "uncommitted"):
                        logger.info("committed leftover changes for %s", task_id)
                    commit_count = len(gitops.commits(task["repo_root"],
                                                      task["base_commit"], task["branch"]))
            except gitops.GitError as exc:
                logger.warning("could not commit leftovers for %s: %s", task_id, exc)

        max_attempts = task["max_attempts"] or conf["max_attempts"]
        mapping = map_exit(code, summary.exists(), task["attempts"] + 1, max_attempts)
        fields = {"note": mapping.note or None}
        if mapping.counted:
            fields["attempts"] = task["attempts"] + 1
        if mapping.status in ("done", "attention"):
            fields["finished_at"] = ended
        spent = (read_runs().get(run_key) or {}).get("spent")
        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET ended_at = ?, exit_code = ?, result = ?, spent = ? "
                "WHERE id = ?",
                (ended, code, mapping.result, spent, run_id),
            )
        tasks.set_status(conn, task_id, mapping.status, **fields)
        return Attempt(task_id, task["title"], code, mapping.result, mapping.status,
                       mapping.counted, mapping.stop_queue, mapping.note, started, ended,
                       str(log_path), commit_count or 0, spent)

    def _prepare(self, conn, task) -> Path:
        """Set up where the task runs; returns the working directory."""
        if not task["repo_root"]:
            return Path(task["project_dir"])
        fields = {}
        base_commit = task["base_commit"]
        if not base_commit:
            base_commit, base_ref = self._base_commit(conn, task)
            fields["base_commit"] = base_commit
            fields["base_ref"] = base_ref
        branch = task["branch"] or f"backlog/{task['id']}"
        worktree = task["worktree"] or str(paths.worktree_dir(task["id"]))
        gitops.ensure_worktree(task["repo_root"], branch, worktree, base_commit)
        if branch != task["branch"]:
            fields["branch"] = branch
        if worktree != task["worktree"]:
            fields["worktree"] = worktree
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            with db.transaction(conn):
                conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?",
                             (*fields.values(), task["id"]))
        cwd = Path(worktree) / (task["subdir"] or "")
        cwd.mkdir(parents=True, exist_ok=True)
        return cwd

    @staticmethod
    def _base_commit(conn, task) -> tuple[str, str | None]:
        """Where a new branch starts: the base branch, or the branch of the task
        this one starts after, unless that is merged into the base branch already.

        Returns the commit, and the other task's branch when it starts from
        that (None when it starts from the base branch).
        """
        root = task["repo_root"]
        base = gitops.resolve_commit(root, task["base_branch"] or "HEAD")
        if not task["after_task"]:
            return base, None
        try:
            before = tasks.get(conn, task["after_task"])
        except tasks.TaskError:
            return base, None
        if not (before["branch"] and gitops.branch_exists(root, before["branch"])):
            return base, None
        tip = gitops.resolve_commit(root, before["branch"])
        if gitops.is_ancestor(root, tip, base):
            return base, None
        return tip, before["branch"]

    def _run_nightshift(self, task_id, cmd, cwd, env, console_log) -> int:
        with open(console_log, "ab") as out:
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=subprocess.STDOUT,
                                        start_new_session=True)
            except OSError as exc:
                out.write(f"could not start nightshift: {exc}\n".encode())
                return FAILED
            with self._lock:
                self.slots[task_id]["proc"] = proc
                stop_now = self.stop_mode == "now"
            if stop_now:
                self._interrupt(proc)
            while True:
                try:
                    code = proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if (self.stop_mode == "now" and self.stop_requested_at
                        and time.time() - self.stop_requested_at > STOP_GRACE_SECONDS):
                    self._kill(proc)
            with self._lock:
                self.slots[task_id]["proc"] = None
            if self.stop_mode == "now" and code != COMPLETED:
                return INTERRUPTED
            return code

    def _kill(self, proc) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def spend_args(spend, parallel) -> list[str]:
    """nightshift's budget options for an attempt.

    ``auto`` with one slot sets no budget: with nothing to split, the
    threshold and the windows per attempt bound the attempt, as before.
    """
    if spend == "auto":
        if parallel <= 1:
            return []
        return ["--spend", "auto", "--spend-share", str(parallel)]
    return ["--spend", spend] if spend else []


def nightshift_available() -> bool:
    cmd = nightshift_command()
    return bool(cmd) and (shutil.which(cmd[0]) is not None or Path(cmd[0]).exists())
