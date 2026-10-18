"""Tasks on the same project: the merge check, the overlap hint, and the
follow-up tasks that resolve a conflict.

For every finished task whose branch is not merged yet, the board checks the
branch against its base branch and against the branches of the other
finished tasks of the same repository. ``git merge-tree`` does this in
memory, and the answers are cached by the commits involved, so they are only
computed again when a branch moves.

Before a task starts, the files its prompt names are compared with the files
of the unmerged tasks that run before it (section 6 of the design, "Tasks on
the same project"). No model is involved in either.
"""

from __future__ import annotations

import threading

from claude_backlog import gitops, pack, paths, tasks

__all__ = ["annotate", "follow_up"]

CHECKED = ("done", "attention")
# A task that has not started yet is compared with tasks in these states.
BEFORE = ("running", "done", "attention")
CACHE_LIMIT = 2000

_cache: dict[tuple, object] = {}
_lock = threading.Lock()


def _cached(key, compute):
    with _lock:
        if key in _cache:
            return _cache[key]
    value = compute()
    with _lock:
        if len(_cache) >= CACHE_LIMIT:
            _cache.clear()
        _cache[key] = value
    return value


def _merged(root, tip, base) -> bool:
    return _cached(("merged", root, tip, base), lambda: gitops.is_ancestor(root, tip, base))


def _conflicts(root, a, b) -> list[str] | None:
    a, b = sorted((a, b))
    return _cached(("conflicts", root, a, b), lambda: gitops.merge_conflicts(root, a, b))


def _changed(root, base, tip) -> frozenset:
    return _cached(("changed", root, base, tip),
                   lambda: frozenset(gitops.changed_names(root, base, tip)))


def _named(root, head, prompt) -> frozenset:
    def compute():
        files = _cached(("tracked", root, head), lambda: gitops.tracked_files(root, head))
        return frozenset(pack.named_files(prompt, files))
    return _cached(("named", root, head, hash(prompt)), compute)


def annotate(cards: list[dict]) -> None:
    """Add the merge check and the overlap hint to the cards.

    ``merged``: the branch is in its base branch. ``included_in``: the branch
    is contained in another finished task's branch ({"task_id", "with"}), so
    it is not checked on its own. ``conflicts``: a list of {"with", "task_id",
    "files"}, the base branch (task_id None) or another task, and the files
    that would conflict. ``overlaps``: for a task that has not started, the
    unmerged tasks before it that change files its prompt names, in the same
    form. ``followers``: the tasks that start after this one.
    """
    by_id = {card["id"]: card for card in cards}
    by_repo: dict[str, list[dict]] = {}
    for card in cards:
        card.setdefault("merged", False)
        card.setdefault("conflicts", [])
        card.setdefault("included_in", None)
        card.setdefault("overlaps", [])
        card.setdefault("followers", [])
        if card.get("repo_root"):
            by_repo.setdefault(card["repo_root"], []).append(card)
    for card in cards:
        before = by_id.get(card.get("after_task"))
        if before is not None:
            before["followers"].append({"task_id": card["id"], "with": card["title"],
                                        "status": card["status"]})
    for root, group in by_repo.items():
        tips = gitops.branch_tips(root)
        _merge_check(root, group, tips)
        _overlap_hint(root, group, tips, by_id)


def _merge_check(root, group, tips) -> None:
    finished = []
    for card in group:
        if card["status"] not in CHECKED or not card.get("branch"):
            continue
        tip = tips.get(card["branch"])
        base_name = card.get("base_branch") or "HEAD"
        base = tips.get(base_name)
        if base is None:
            try:
                base = gitops.resolve_commit(root, base_name)
            except gitops.GitError:
                base = None
        if tip is None or base is None:
            continue
        if tip == card.get("base_commit"):
            continue  # no commits of its own yet
        if _merged(root, tip, base):
            card["merged"] = True
            continue
        finished.append((card, tip, base, base_name))
    # A branch that another finished branch contains is merged with that one.
    for card, tip, _, _ in finished:
        for other, other_tip, _, _ in finished:
            if other is not card and other_tip != tip and _merged(root, tip, other_tip):
                card["included_in"] = {"task_id": other["id"], "with": other["title"]}
                break
    open_ = [(card, tip) for card, tip, base, base_name in finished
             if not card["included_in"]]
    for card, tip, base, base_name in finished:
        if card["included_in"]:
            continue
        files = _conflicts(root, base, tip)
        if files:
            card["conflicts"].append({"with": base_name, "task_id": None, "files": files})
    for i, (card, tip) in enumerate(open_):
        for other, other_tip in open_[i + 1:]:
            files = _conflicts(root, tip, other_tip)
            if files:
                card["conflicts"].append({"with": other["title"],
                                          "task_id": other["id"], "files": files})
                other["conflicts"].append({"with": card["title"],
                                           "task_id": card["id"], "files": files})


def _not_started(card) -> bool:
    return (card["status"] in ("queued", "hold") and not card.get("attempts")
            and not card.get("after_task") and bool((card.get("prompt") or "").strip()))


def _leads_to(card, target_id, by_id) -> bool:
    """Whether ``card`` starts after ``target_id``, directly or through others."""
    seen = set()
    while card is not None and card.get("after_task") and card["id"] not in seen:
        if card["after_task"] == target_id:
            return True
        seen.add(card["id"])
        card = by_id.get(card["after_task"])
    return False


def _overlap_hint(root, group, tips, by_id) -> None:
    waiting = [card for card in group if _not_started(card)]
    if not waiting:
        return
    try:
        head = gitops.resolve_commit(root, "HEAD")
    except gitops.GitError:
        return  # a repository without commits

    def files_of(card) -> frozenset:
        files = frozenset()
        tip = tips.get(card.get("branch") or "")
        if tip and card.get("base_commit") and tip != card["base_commit"]:
            files = _changed(root, card["base_commit"], tip)
        if card["status"] in CHECKED:
            return files
        return files | _named(root, head, card.get("prompt") or "")

    queued = [card["id"] for card in group if card["status"] == "queued"]
    for card in waiting:
        mine = _named(root, head, card["prompt"])
        if not mine:
            continue
        for other in group:
            if other is card or other["merged"] or other["included_in"]:
                continue
            if other["status"] == "queued":
                if card["status"] == "queued" and (
                        queued.index(other["id"]) > queued.index(card["id"])):
                    continue
            elif other["status"] not in BEFORE:
                continue
            if _leads_to(other, card["id"], by_id):
                continue
            shared = sorted(mine & files_of(other))
            if shared:
                card["overlaps"].append({"with": other["title"], "task_id": other["id"],
                                         "files": shared})


# --- follow-up tasks ("Update with Claude", "Combine with Claude") --------------

FOLLOW_UP_PROMPT = """\
## Goal
Make the work of the task "{title}" merge cleanly with {what}. This
worktree's branch starts from the end of {branch}, that task's branch.
Merge {merge_ref} into it and resolve the conflicts, so that both sides
keep what they meant to do.

## Context
- Merging them conflicts in: {files}.
- What the task "{title}" did: {summary}{other_summary}
- `git log` and `git diff` on each side show what changed and why.

## Constraints
- Do not drop or undo the work of either side. Where both changed the same
  lines, combine the changes; where they really contradict each other,
  keep the intent of both as far as possible and explain the choice in
  SUMMARY.md under "Decisions for me".
- No changes beyond what the merge needs: no new features or refactoring.

## Done when
- `git merge {merge_ref}` is committed on this branch, and
  `git merge-base --is-ancestor {merge_ref} HEAD` succeeds.
- No conflict markers are left (`git grep -n -e '^<<<<<<<' -e '^>>>>>>>'`
  finds nothing).
- The project's tests and checks pass, as before the merge.

## When unsure
If the merge has nothing to do (the branch already contains {merge_ref}),
say so in SUMMARY.md and finish.
"""


def follow_up(conn, task_id, other_id=None) -> dict:
    """Queue a task that merges the base branch (or the branch of ``other_id``)
    into a branch that starts from the done task ``task_id``."""
    task = tasks.get(conn, task_id)
    root = task["repo_root"]
    if task["status"] != "done":
        raise tasks.TaskError("only a done task's branch can be updated or combined")
    if not (root and task["branch"] and gitops.branch_exists(root, task["branch"])):
        raise tasks.TaskError("this task has no branch")
    for other in tasks.list_tasks(conn):
        if other["after_task"] == task_id:
            raise tasks.TaskError(f'"{other["title"]}" already builds on this branch')
    other_summary = ""
    if other_id:
        other = tasks.get(conn, other_id)
        if other["repo_root"] != root:
            raise tasks.TaskError("the other task is in another repository")
        if other["status"] != "done":
            raise tasks.TaskError("only the branch of a done task can be combined")
        if not (other["branch"] and gitops.branch_exists(root, other["branch"])):
            raise tasks.TaskError("the other task has no branch")
        merge_ref = other["branch"]
        what = f'the work of the task "{other["title"]}" (its branch {merge_ref})'
        title = f'Combine "{task["title"]}" with "{other["title"]}"'
        other_summary = (f'\n- What the task "{other["title"]}" did: '
                         f"{_summary_ref(other['id'])}")
    else:
        merge_ref = task["base_branch"] or ""
        if not merge_ref or merge_ref == "HEAD":
            raise tasks.TaskError("this task has no base branch to update from")
        what = merge_ref
        title = f'Update "{task["title"]}" with {merge_ref}'
    files = gitops.merge_conflicts(root, task["branch"], merge_ref) or []
    prompt = FOLLOW_UP_PROMPT.format(
        title=task["title"], what=what, branch=task["branch"], merge_ref=merge_ref,
        files=", ".join(files) if files else "(none found now; check again)",
        summary=_summary_ref(task["id"]), other_summary=other_summary)
    return tasks.create(conn, prompt, task["project_dir"], title=title,
                        priority=task["priority"], model=task["model"],
                        effort=task["effort"], base_branch=task["base_branch"],
                        after_task=task_id, merge_ref=merge_ref)


def _summary_ref(task_id) -> str:
    path = paths.task_dir(task_id) / "SUMMARY.md"
    return f"its summary is in {path}." if path.exists() else "see its commits."
