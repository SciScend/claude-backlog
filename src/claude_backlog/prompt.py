"""The unattended preamble and the prompt built from it."""

from __future__ import annotations

import re

__all__ = ["build_prompt", "decisions_count"]

INTRO = """\
This is an unattended run. Nobody will answer questions or approve anything.

- Do not ask questions and do not wait for input. When something is unclear,
  make a reasonable decision yourself (following the task's "When unsure"
  section, if it has one) and write it down in {task_dir}/STATE.md with the
  reason.
- Read {task_dir}/STATE.md first if it exists. It records what earlier
  sessions did. Trust it and continue from where it says.
- The work may take several sessions: your context is compacted when it
  fills up, and the task is resumed after usage limits. Do not stop early
  or cut corners to finish within one session.
- Keep {task_dir}/STATE.md current as you work. If you hit a session or
  usage limit, or are about to, first record the exact progress there: what
  is done, what is half done, and the next step, so the work can be resumed.
"""

GIT_RULES = """\
- You are working in a separate git worktree on the branch {branch}, started
  from {base_ref} at {base_commit}. It is a fresh checkout: install
  dependencies if the task needs them, and expect files that are not in git
  (for example .env) to be missing.
- Work in small steps. After each step, run the relevant checks and commit
  on {branch} with a clear message, so every commit leaves the project
  working. Do not push, do not switch or create branches, do not merge.
- Only change files inside this worktree and {task_dir}. Do not touch
  anything else, including the original project folder {project_dir}.
"""

# Added when the branch started from the branch of an earlier task.
AFTER_RULE = """\
- {base_ref} is the branch of the task "{after_title}", which ran before
  this one and is not merged into {base_branch} yet. Its work is already in
  this worktree: build on it, and do not undo it.
"""

# Added for a task that resolves a merge conflict (the board's "Update with
# Claude" and "Combine with Claude").
MERGE_RULE = """\
- This task merges {merge_ref} into {branch}: that one merge is allowed,
  and it is the task itself.
"""

PLAIN_RULES = """\
- Only change files inside {project_dir} and {task_dir}. Do not touch
  anything else.
- Work in small steps, and run the relevant checks after each one.
- Do not commit, push, or open pull requests.
"""

OUTRO = """\
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
- When the task is done, write {task_dir}/SUMMARY.md: what was done, the
  checks you ran and their results, problems left open, and a section
  "Decisions for me". Mark everything done in STATE.md. Write SUMMARY.md
  only when the task is really finished; it is the signal that the task is
  complete.

The task:

"""


def build_prompt(task: dict, task_dir, after_title=None) -> str:
    """The full prompt for one attempt: preamble plus the task's own prompt.

    ``after_title`` is the title of the task whose branch this one started from.
    """
    values = {
        "task_dir": str(task_dir),
        "project_dir": task["project_dir"],
        "branch": task.get("branch") or "",
        "base_branch": task.get("base_branch") or "",
        "base_ref": task.get("base_ref") or task.get("base_branch") or "",
        "base_commit": (task.get("base_commit") or "")[:12],
        "after_title": after_title or "",
        "merge_ref": task.get("merge_ref") or "",
    }
    rules = PLAIN_RULES
    if task.get("worktree"):
        rules = GIT_RULES
        if task.get("base_ref") and after_title:
            rules += AFTER_RULE
        if task.get("merge_ref"):
            rules += MERGE_RULE
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
