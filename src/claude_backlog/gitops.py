"""The git side of a task: its repository, branch and worktree."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GitError",
    "RepoInfo",
    "repo_info",
    "is_dirty",
    "resolve_commit",
    "is_ancestor",
    "branch_tips",
    "merge_conflicts",
    "ensure_worktree",
    "has_identity",
    "identity_env",
    "commit_all",
    "commits",
    "diff_stat",
    "diff",
    "remove_worktree",
    "delete_branch",
]

# Used for commits only when the repository has no identity of its own.
FALLBACK_NAME = "claude-backlog"
FALLBACK_EMAIL = "claude-backlog@localhost"

MAX_DIFF_BYTES = 2_000_000


class GitError(RuntimeError):
    pass


def git(*args, cwd=None, check=True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, errors="replace"
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip() or result.stdout}")
    return result.stdout.strip()


@dataclass
class RepoInfo:
    root: str
    subdir: str  # the project folder relative to the root, "" for the root
    branch: str  # the branch checked out now, "" when detached


def repo_info(folder) -> RepoInfo | None:
    """The repository ``folder`` belongs to, or None when it is not in one."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    try:
        root = git("rev-parse", "--show-toplevel", cwd=folder)
        prefix = git("rev-parse", "--show-prefix", cwd=folder)
    except (GitError, FileNotFoundError):
        return None
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=folder, check=False)
    return RepoInfo(root=root, subdir=prefix.rstrip("/"), branch=branch)


def is_dirty(folder) -> bool:
    return bool(git("status", "--porcelain", cwd=folder, check=False))


def resolve_commit(repo_root, ref) -> str:
    return git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=repo_root)


def is_ancestor(repo_root, commit, other) -> bool:
    """True when ``commit`` is already part of ``other`` (merged into it)."""
    result = subprocess.run(["git", "merge-base", "--is-ancestor", commit, other],
                            cwd=repo_root, capture_output=True)
    return result.returncode == 0


def branch_tips(repo_root) -> dict[str, str]:
    """Every local branch and the commit it points to."""
    out = git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads",
              cwd=repo_root, check=False)
    return dict(line.rsplit(" ", 1) for line in out.splitlines() if " " in line)


def merge_conflicts(repo_root, ours, theirs) -> list[str] | None:
    """The files that would conflict when merging two commits; [] when none.

    Merges in memory with ``git merge-tree --write-tree`` (git 2.38 or later),
    so no file is touched. None when it cannot tell (an older git, a bad commit).
    """
    result = subprocess.run(
        ["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", ours, theirs],
        cwd=repo_root, capture_output=True, text=True, errors="replace",
    )
    lines = result.stdout.splitlines()
    if result.returncode == 0:
        return []
    if result.returncode == 1 and lines and len(lines[0]) >= 40 and all(
            c in "0123456789abcdef" for c in lines[0]):
        return [line for line in lines[1:] if line]
    return None


def branch_exists(repo_root, branch) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_root,
    )
    return result.returncode == 0


def ensure_worktree(repo_root, branch, path, base_commit) -> None:
    """Make sure ``path`` is a worktree of ``branch``, creating either as needed."""
    path = Path(path)
    if (path / ".git").exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "prune", cwd=repo_root, check=False)
    if branch_exists(repo_root, branch):
        git("worktree", "add", str(path), branch, cwd=repo_root)
    else:
        git("worktree", "add", "-b", branch, str(path), base_commit, cwd=repo_root)


def has_identity(folder) -> bool:
    return bool(git("config", "user.email", cwd=folder, check=False))


def identity_env(folder) -> dict:
    """Environment that lets commits work in a repository without an identity."""
    if has_identity(folder):
        return {}
    return {
        "GIT_AUTHOR_NAME": FALLBACK_NAME,
        "GIT_AUTHOR_EMAIL": FALLBACK_EMAIL,
        "GIT_COMMITTER_NAME": FALLBACK_NAME,
        "GIT_COMMITTER_EMAIL": FALLBACK_EMAIL,
    }


def commit_all(folder, message) -> bool:
    """Commit everything left in the worktree. Returns True if there was anything."""
    if not is_dirty(folder):
        return False
    git("add", "-A", cwd=folder)
    args = []
    if not has_identity(folder):
        args = ["-c", f"user.name={FALLBACK_NAME}", "-c", f"user.email={FALLBACK_EMAIL}"]
    git(*args, "commit", "--no-verify", "-q", "-m", message, cwd=folder)
    return True


def commits(repo_root, base_commit, branch) -> list[dict]:
    """Commits on ``branch`` since ``base_commit``, newest first."""
    if not (base_commit and branch_exists(repo_root, branch)):
        return []
    out = git("log", "--format=%H%x1f%h%x1f%ct%x1f%s", f"{base_commit}..{branch}",
              cwd=repo_root, check=False)
    result = []
    for line in out.splitlines():
        full, short, when, subject = line.split("\x1f", 3)
        result.append({"sha": full, "short": short, "time": int(when), "subject": subject})
    return result


def diff_stat(repo_root, base_commit, branch) -> list[dict]:
    if not (base_commit and branch_exists(repo_root, branch)):
        return []
    out = git("diff", "--numstat", base_commit, branch, cwd=repo_root, check=False)
    files = []
    for line in out.splitlines():
        added, removed, name = line.split("\t", 2)
        files.append({
            "file": name,
            "added": None if added == "-" else int(added),
            "removed": None if removed == "-" else int(removed),
        })
    return files


def diff(repo_root, base_commit, branch) -> str:
    if not (base_commit and branch_exists(repo_root, branch)):
        return ""
    out = git("diff", base_commit, branch, cwd=repo_root, check=False)
    if len(out) > MAX_DIFF_BYTES:
        out = out[:MAX_DIFF_BYTES] + "\n\n[diff truncated]\n"
    return out


def remove_worktree(repo_root, path) -> None:
    if Path(path).exists():
        git("worktree", "remove", "--force", str(path), cwd=repo_root, check=False)
    git("worktree", "prune", cwd=repo_root, check=False)


def delete_branch(repo_root, branch) -> None:
    if branch_exists(repo_root, branch):
        git("branch", "-D", branch, cwd=repo_root)
