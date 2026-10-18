"""The merge check: would a finished task's branch merge cleanly?

For every finished task whose branch is not merged yet, the board checks the
branch against its base branch and against the branches of the other
finished tasks of the same repository. ``git merge-tree`` does this in
memory, and the answers are cached by the commits involved, so they are only
computed again when a branch moves.
"""

from __future__ import annotations

import threading

from claude_backlog import gitops

__all__ = ["annotate"]

CHECKED = ("done", "attention")
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


def annotate(cards: list[dict]) -> None:
    """Add ``merged`` and ``conflicts`` to the cards of finished tasks.

    ``conflicts`` is a list of {"with", "task_id", "files"}: the base branch
    (task_id None) or another task, and the files that would conflict.
    """
    by_repo: dict[str, list[dict]] = {}
    for card in cards:
        card.setdefault("merged", False)
        card.setdefault("conflicts", [])
        if card["status"] in CHECKED and card.get("branch") and card.get("repo_root"):
            by_repo.setdefault(card["repo_root"], []).append(card)
    for root, group in by_repo.items():
        tips = gitops.branch_tips(root)
        open_ = []
        for card in group:
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
            files = _conflicts(root, base, tip)
            if files:
                card["conflicts"].append({"with": base_name, "task_id": None,
                                          "files": files})
            open_.append((card, tip))
        for i, (card, tip) in enumerate(open_):
            for other, other_tip in open_[i + 1:]:
                files = _conflicts(root, tip, other_tip)
                if files:
                    card["conflicts"].append({"with": other["title"],
                                              "task_id": other["id"], "files": files})
                    other["conflicts"].append({"with": card["title"],
                                               "task_id": card["id"], "files": files})
