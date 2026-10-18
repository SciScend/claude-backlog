"""The runner: works through the queue, one task at a time, through nightshift."""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
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
from claude_backlog.usage import nightshift_command

__all__ = ["Runner", "Attempt", "recover", "map_exit"]

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
    """Runs the queue in a background thread. One instance per service."""

    def __init__(self, db_path=None, inhibitor=None, notifier=notify, on_change=None):
        self.db_path = db_path
        self.inhibitor = inhibitor or Inhibitor()
        self.notifier = notifier
        self.on_change = on_change or (lambda: None)
        self._lock = threading.RLock()
        self._thread = None
        self.proc = None
        self.state = "idle"  # idle, running, stopping
        self.trigger = None  # run-now, away
        self.until = None
        self.started_at = None
        self.task_id = None
        self.task_started_at = None
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
                self._interrupt()
            self._changed()
            return True

    def _interrupt(self) -> None:
        proc = self.proc
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

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "trigger": self.trigger,
                "until": self.until,
                "started_at": self.started_at,
                "task_id": self.task_id,
                "task_started_at": self.task_started_at,
                "stop_mode": self.stop_mode,
                "last_stop": self.last_stop,
                "inhibit": self.inhibitor.available,
            }

    # --- the loop ---------------------------------------------------------

    def _loop(self) -> None:
        conn = db.connect(self.db_path)
        attempts = []
        tried = set()
        reason = None
        started = time.time()
        trigger = self.trigger
        try:
            self.inhibitor.start("working through the claude-backlog queue")
            while True:
                if self.stop_mode:
                    reason = ("you came back" if self.stop_cause == "back"
                              else "stopped by you")
                    break
                if self.until and time.time() >= self.until:
                    reason = "the 'until' time has passed"
                    break
                task = self._pick(conn, tried)
                if task is None:
                    reason = ("the queue is empty" if not tasks.queue(conn) else
                              "the tasks left in the queue wait for other tasks")
                    break
                tried.add(task["id"])
                attempt = self._run_task(conn, task)
                attempts.append(attempt)
                if attempt.stop_queue:
                    if attempt.result == "interrupted":
                        reason = ("you came back" if self.stop_cause == "back"
                                  else "stopped by you")
                    else:
                        reason = attempt.note
                    break
        except Exception as exc:  # the service must survive anything a run does
            logger.exception("the runner failed")
            reason = f"internal error: {exc}"
        finally:
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
                self.proc = None
                self.task_id = None
                self.task_started_at = None
                self.last_stop = {"reason": reason, "at": ended, "trigger": trigger,
                                  "tasks": len(attempts)}
                self._changed()

    @staticmethod
    def _pick(conn, tried):
        """The next task: each queued task gets a turn before any gets a second one.

        A task that used up its windows goes back to its place in the queue, so
        without this it would run again at once and other tasks would wait.
        When every queued task has had its turn, a new round begins.
        """
        queued = tasks.runnable(conn)
        if not queued:
            return None
        for task in queued:
            if task["id"] not in tried:
                return task
        tried.clear()
        return queued[0]

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

        cmd = [*nightshift_command(), "-f", str(prompt_file),
               "-t", f"{task['threshold'] or conf['threshold']:g}",
               "--status-file", str(tdir / "nightshift-status.md"),
               "--log-file", str(log_path)]
        windows = task["max_windows"] or conf["max_windows"]
        if windows:
            cmd += ["-w", str(windows)]
        model = task["model"] or conf["model"]
        if model:
            cmd += ["-m", model]
        effort = task["effort"] or conf["effort"]
        if effort:
            cmd += ["-e", effort]

        env = dict(os.environ)
        if task["worktree"]:
            env.update(gitops.identity_env(cwd))
        with self._lock:
            self.task_id = task_id
            self.task_started_at = started
            self._changed()
        console = tdir / "logs" / f"{run_id}.console.log"
        code = self._run_nightshift(cmd, cwd, env, console)
        ended = time.time()
        if code in KNOWN_EXITS:
            # nightshift logged everything to the log file as well; the console
            # copy only matters when it crashed.
            console.unlink(missing_ok=True)

        commit_count = None
        if task["worktree"]:
            try:
                if gitops.commit_all(task["worktree"], f"backlog: commit what attempt "
                                     f"{task['attempts'] + 1} left uncommitted"):
                    logger.info("committed leftover changes for %s", task_id)
                commit_count = len(gitops.commits(task["repo_root"], task["base_commit"],
                                                    task["branch"]))
            except gitops.GitError as exc:
                logger.warning("could not commit leftovers for %s: %s", task_id, exc)

        max_attempts = task["max_attempts"] or conf["max_attempts"]
        mapping = map_exit(code, summary.exists(), task["attempts"] + 1, max_attempts)
        fields = {"note": mapping.note or None}
        if mapping.counted:
            fields["attempts"] = task["attempts"] + 1
        if mapping.status in ("done", "attention"):
            fields["finished_at"] = ended
        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET ended_at = ?, exit_code = ?, result = ? WHERE id = ?",
                (ended, code, mapping.result, run_id),
            )
        tasks.set_status(conn, task_id, mapping.status, **fields)
        return Attempt(task_id, task["title"], code, mapping.result, mapping.status,
                       mapping.counted, mapping.stop_queue, mapping.note, started, ended,
                       str(log_path), commit_count or 0)

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

    def _run_nightshift(self, cmd, cwd, env, console_log) -> int:
        with open(console_log, "ab") as out:
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=subprocess.STDOUT,
                                        start_new_session=True)
            except OSError as exc:
                out.write(f"could not start nightshift: {exc}\n".encode())
                return FAILED
            with self._lock:
                self.proc = proc
                stop_now = self.stop_mode == "now"
            if stop_now:
                self._interrupt()
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
                self.proc = None
            if self.stop_mode == "now" and code != COMPLETED:
                return INTERRUPTED
            return code

    def _kill(self, proc) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def nightshift_available() -> bool:
    cmd = nightshift_command()
    return bool(cmd) and (shutil.which(cmd[0]) is not None or Path(cmd[0]).exists())
