"""The unattended preamble and the prompt built from it."""

from __future__ import annotations

import re

__all__ = ["build_prompt", "decisions_count"]

INTRO = """\
This is an unattended run. Nobody will answer questions or approve anything.

- Do not ask questions and do not wait for input. When something is unclear,
  make a reasonable decision yourself and write it down in {task_dir}/STATE.md
  with the reason.
- Read {task_dir}/STATE.md first if it exists. It records what earlier
  sessions did. Trust it and continue from where it says.
- Keep {task_dir}/STATE.md current as you work. If you hit a session or
  usage limit, or are about to, first record the exact progress there: what
  is done, what is half done, and the next step, so the work can be resumed.
"""

GIT_RULES = """\
- You are working in a separate git worktree on the branch {branch}, started
  from {base_branch} at {base_commit}. It is a fresh checkout: install
  dependencies if the task needs them, and expect files that are not in git
  (for example .env) to be missing.
- Commit your work on {branch} whenever a step is complete, with a clear
  message. Do not push, do not switch or create branches, do not merge.
- Only change files inside this worktree and {task_dir}. Do not touch
  anything else, including the original project folder {project_dir}.
"""

PLAIN_RULES = """\
- Only change files inside {project_dir} and {task_dir}. Do not touch
  anything else.
- Do not commit, push, or open pull requests.
"""

OUTRO = """\
- Do not end your turn while any agent you started is still running. Wait
  for all of them to finish.
- When the task is done, write {task_dir}/SUMMARY.md: what was done, checks
  and their results, problems left open, and a section "Decisions for me".
  Mark everything done in STATE.md. Write SUMMARY.md only when the task is
  really finished; it is the signal that the task is complete.

The task:

"""


def build_prompt(task: dict, task_dir) -> str:
    """The full prompt for one attempt: preamble plus the task's own prompt."""
    values = {
        "task_dir": str(task_dir),
        "project_dir": task["project_dir"],
        "branch": task.get("branch") or "",
        "base_branch": task.get("base_branch") or "",
        "base_commit": (task.get("base_commit") or "")[:12],
    }
    rules = GIT_RULES if task.get("worktree") else PLAIN_RULES
    return (INTRO + rules + OUTRO).format(**values) + task["prompt"].strip() + "\n"


_LIST_ITEM = re.compile(r"^([-*+]|\d+[.)])\s+\S")


def decisions_count(summary: str) -> int:
    """How many items the "Decisions for me" section of SUMMARY.md lists."""
    count = 0
    inside = False
    for line in summary.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            inside = "decisions for me" in stripped.lower()
            continue
        if inside and _LIST_ITEM.match(stripped):
            count += 1
    return count
