"""The board's "Merge into <base branch>" button.

It merges a finished task's branch into its base branch in the user's own
checkout, the one place backlog otherwise never writes to. So it only does
that when nothing can go wrong there: the base branch is checked out, no
tracked file has uncommitted changes, nothing is half done (a merge, a
rebase), and ``git merge-tree`` finds no conflict. Then it merges with a
merge commit, the repository's hooks and the user's git identity, and
aborts the merge if anything fails. It never pushes.

``check`` says whether the button can be used, and why not; ``merge`` runs
the same checks again right before merging.
"""

from __future__ import annotations

import threading

from claude_backlog import gitops, tasks

__all__ = ["check", "merge"]

MERGEABLE = ("done", "attention")

_lock = threading.Lock()


def check(task) -> dict | None:
    """Whether the task's branch can be merged from the board.

    None when the board has nothing to offer (no branch, not finished,
    already merged). Otherwise {"base", "ok", "reason", "code"}: ``ok`` True,
    or False with the reason in words and a short code ("conflict",
    "dirty", "checkout", "in-progress", "unknown", "changed").
    """
    root, branch = task.get("repo_root"), task.get("branch")
    if task.get("status") not in MERGEABLE or not (root and branch):
        return None
    if not gitops.branch_exists(root, branch):
        return None
    base = task.get("base_branch") or ""
    result = {"base": base, "ok": False, "reason": None, "code": None}

    def no(code, reason):
        result.update(code=code, reason=reason)
        return result

    if not base or base == "HEAD":
        return no("checkout", "The task started on a detached HEAD, so it has no base "
                              "branch to merge into. Merge it by hand.")
    tip = gitops.resolve_commit(root, branch)
    if tip == task.get("base_commit"):
        return None  # no commits of its own
    try:
        base_tip = gitops.resolve_commit(root, base)
    except gitops.GitError:
        return no("checkout", f"The base branch {base} does not exist any more.")
    if gitops.is_ancestor(root, tip, base_tip):
        return None  # merged already
    info = gitops.repo_info(root)
    current = info.branch if info else ""
    if current != base:
        what = f"{current} is checked out" if current else "no branch is checked out"
        return no("checkout", f"In {root}, {what}, not {base}. Check out {base} "
                              "there to merge from the board.")
    doing = gitops.operation_in_progress(root)
    if doing:
        return no("in-progress", f"{doing.capitalize()} is not finished in {root}. "
                                 "Finish or abort it first.")
    changed = gitops.tracked_changes(root)
    if changed:
        files = "file has" if changed == 1 else "files have"
        return no("dirty", f"{changed} {files} uncommitted changes in {root}. Commit "
                           "or stash them first.")
    files = gitops.merge_conflicts(root, base_tip, tip)
    if files is None:
        return no("unknown", "Git cannot check this merge for conflicts here (git "
                             "2.38 or later is needed). Merge it by hand.")
    if files:
        return no("conflict", f"Merging would conflict in {', '.join(files)}.")
    result["ok"] = True
    return result


def merge(conn, task_id, tip=None) -> dict:
    """Merge the task's branch into its base branch in the user's checkout.

    ``tip`` is the branch commit the user saw when they confirmed; when the
    branch has moved since, nothing is merged. Returns {"commit", "base"}.
    """
    with _lock:
        task = tasks.get(conn, task_id)
        if task["status"] not in MERGEABLE:
            raise tasks.TaskError("only a done task, or one that needs attention, can "
                                  "be merged")
        state = check(task)
        if state is None:
            raise tasks.TaskError("there is nothing to merge: the branch is merged "
                                  "already, or has no commits of its own")
        if tip and gitops.resolve_commit(task["repo_root"], task["branch"]) != tip:
            raise tasks.TaskError("the branch has changed since you looked at it; "
                                  "look at it again")
        if not state["ok"]:
            raise tasks.TaskError(state["reason"])
        message = f"Merge {task['branch']}\n\n{task['title']}"
        commit = gitops.merge_into_head(task["repo_root"], task["branch"], message)
        return {"commit": commit, "base": state["base"]}
