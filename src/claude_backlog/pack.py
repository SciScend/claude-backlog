"""The project pack: what Claude needs to know about a project to write a
task's prompt, gathered in Python without a model.

The pack holds the file list, the start of README, CLAUDE.md and AGENTS.md,
the build and test commands, and the files that the description names or
that match its key words. Everything but the matching files is cached per
project folder and reused while git HEAD stays the same.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from claude_backlog import gitops

__all__ = ["build", "clear_cache", "keywords"]

MAX_FILES = 2000           # paths in the file list
LIST_CHARS = 40_000        # the file list as a whole
DOC_CHARS = 6_000          # the start of README, CLAUDE.md and AGENTS.md, each
PYPROJECT_CHARS = 4_000
BUILD_CHARS = 8_000        # the build and test commands together
MATCH_CHARS = 60_000       # the matching files together
FILE_CHARS = 15_000        # one matching file
MAX_MATCHES = 12           # matching files shown with their content
MAX_MORE = 20              # further matching files, named only
MAX_KEYWORDS = 12
SEARCH_FILES = 5_000       # files searched for the key words
SEARCH_BYTES = 300_000     # larger files are not searched

SKIP_DIRS = {"node_modules", "__pycache__", "venv", "site-packages", "dist", "build",
             "target"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock",
              "poetry.lock", "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum"}
README_NAMES = ("README.md", "README.rst", "README.txt", "README")
GUIDE_NAMES = ("CLAUDE.md", "AGENTS.md")
MAKEFILES = ("Makefile", "makefile", "GNUmakefile")

STOPWORDS = {
    "about", "above", "after", "again", "also", "always", "another", "anything",
    "around", "because", "been", "before", "being", "below", "between", "both",
    "cannot", "change", "could", "does", "doing", "done", "down", "during", "each",
    "either", "else", "even", "every", "everything", "from", "have", "having", "here",
    "into", "just", "keep", "know", "like", "made", "make", "makes", "many", "more",
    "most", "much", "must", "need", "needs", "never", "only", "other", "over", "same",
    "should", "since", "some", "something", "still", "such", "sure", "than", "that",
    "their", "them", "then", "there", "these", "they", "thing", "things", "this",
    "those", "through", "under", "until", "upon", "very", "want", "when", "where",
    "whether", "which", "while", "will", "with", "within", "without", "work", "would",
    "your", "file", "files", "code", "please", "task", "tasks", "using", "used",
}


@dataclass
class _Base:
    head: str
    files: list[str]
    text: str


_cache: dict[str, _Base] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def build(project_dir, description: str) -> str:
    """The pack for a task in ``project_dir`` described by ``description``."""
    folder = Path(project_dir).expanduser().resolve()
    base = _base(folder)
    return "\n\n".join(part for part in (base.text, _matches(folder, base.files,
                                                              description)) if part)


# --- the cached part -------------------------------------------------------------


def _head(folder: Path) -> str:
    try:
        return gitops.git("rev-parse", "HEAD", cwd=folder, check=False)
    except FileNotFoundError:  # git is not installed
        return ""


def _base(folder: Path) -> _Base:
    head = _head(folder)
    key = str(folder)
    with _lock:
        cached = _cache.get(key)
    if head and cached and cached.head == head:
        return cached
    files = _file_list(folder, bool(head))
    parts = [_files_text(files), *_docs(folder), _build_commands(folder)]
    base = _Base(head, files, "\n\n".join(p for p in parts if p))
    if head:
        with _lock:
            _cache[key] = base
    return base


def _file_list(folder: Path, in_git: bool) -> list[str]:
    if in_git:
        out = gitops.git("ls-files", "-z", cwd=folder, check=False)
        return [p for p in out.split("\0") if p]
    found = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        rel = Path(root).relative_to(folder)
        found += [str(rel / n) if str(rel) != "." else n
                  for n in sorted(names) if not n.startswith(".")]
        if len(found) > SEARCH_FILES:
            break
    return found


def _files_text(files: list[str]) -> str:
    shown = []
    size = 0
    for path in files[:MAX_FILES]:
        size += len(path) + 1
        if size > LIST_CHARS:
            break
        shown.append(path)
    head = f"## Files ({len(files)})"
    if len(shown) < len(files):
        head = f"## Files (the first {len(shown)} of {len(files)})"
    return head + "\n" + "\n".join(shown) if files else "## Files\n(none)"


def _read(path: Path, limit: int) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(limit + 1)
    except OSError:
        return None
    if len(text) > limit:
        text = text[:limit] + f"\n[... cut after {limit} characters]"
    return text.strip()


def _docs(folder: Path) -> list[str]:
    parts = []
    readme = next((n for n in README_NAMES if (folder / n).is_file()), None)
    for name in ([readme] if readme else []) + list(GUIDE_NAMES):
        text = _read(folder / name, DOC_CHARS) if (folder / name).is_file() else None
        if text:
            parts.append(f"## {name}\n{text}")
    return parts


def _package_scripts(folder: Path) -> str:
    try:
        data = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict) or not scripts:
        return ""
    return "package.json scripts:\n" + "\n".join(
        f"  npm run {name}: {cmd}" for name, cmd in scripts.items())


_TARGET = re.compile(r"^([A-Za-z0-9][\w.-]*)\s*:(?!=)")


def _make_targets(folder: Path) -> str:
    name = next((n for n in MAKEFILES if (folder / n).is_file()), None)
    text = _read(folder / name, 100_000) if name else None
    if not text:
        return ""
    targets = list(dict.fromkeys(m.group(1) for line in text.splitlines()
                                 if (m := _TARGET.match(line))))
    return f"{name} targets: " + ", ".join(f"make {t}" for t in targets) if targets else ""


def _run_steps(text: str) -> list[str]:
    """The commands of the ``run:`` steps in a CI workflow file."""
    steps = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", lines[i])
        i += 1
        if not m:
            continue
        indent, value = len(m.group(1)), m.group(2).strip()
        if value and value[0] not in "|>":
            steps.append(value)
            continue
        block = []
        while i < len(lines) and (not lines[i].strip()
                                  or len(lines[i]) - len(lines[i].lstrip()) > indent):
            if lines[i].strip():
                block.append(lines[i].strip())
            i += 1
        steps += block
    return steps


def _ci_steps(folder: Path) -> list[str]:
    workflows = folder / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    parts = []
    for path in sorted(workflows.iterdir()):
        if path.suffix not in (".yml", ".yaml"):
            continue
        steps = _run_steps(_read(path, 100_000) or "")
        if steps:
            parts.append(f".github/workflows/{path.name} run steps:\n"
                         + "\n".join(f"  {s}" for s in steps))
    return parts


def _build_commands(folder: Path) -> str:
    parts = []
    pyproject = _read(folder / "pyproject.toml", PYPROJECT_CHARS)
    if pyproject:
        parts.append(f"pyproject.toml:\n{pyproject}")
    parts += [_package_scripts(folder), _make_targets(folder), *_ci_steps(folder)]
    text = "\n\n".join(p for p in parts if p)
    if not text:
        return ""
    if len(text) > BUILD_CHARS:
        text = text[:BUILD_CHARS] + f"\n[... cut after {BUILD_CHARS} characters]"
    return "## Build and test commands\n" + text


# --- the part that depends on the description -----------------------------------


def keywords(description: str) -> list[str]:
    """The words of the description worth searching for, in order."""
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", description)
    found = dict.fromkeys(w.lower() for w in words if w.lower() not in STOPWORDS)
    return list(found)[:MAX_KEYWORDS]


def _named(description: str, files: list[str]) -> set[str]:
    """Files the description names, by path or by file name."""
    tokens = {t.strip(".,;:()[]{}'\"`") for t in description.split()}
    tokens = {t.lstrip("./") for t in tokens if "." in t or "/" in t}
    tokens.discard("")
    named = set()
    for path in files:
        name = path.rsplit("/", 1)[-1]
        if path in tokens or name in tokens or any(path.endswith("/" + t) for t in tokens):
            named.add(path)
    return named


def _searchable(folder: Path, path: str) -> str | None:
    """The file's text in lower case: "" when it is too large to search, None
    when it is binary or cannot be read."""
    full = folder / path
    try:
        if not full.is_file():
            return None
        if full.stat().st_size > SEARCH_BYTES:
            return ""
        data = full.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace").lower()


def _matches(folder: Path, files: list[str], description: str) -> str:
    words = keywords(description)
    named = _named(description, files)
    scores = {}
    for path in files[:SEARCH_FILES]:
        if path.rsplit("/", 1)[-1] in SKIP_FILES:
            continue
        lower = path.lower()
        score = 10 * (path in named) + 3 * sum(w in lower for w in words)
        if words:
            text = _searchable(folder, path)
            if text is None and path not in named:
                continue
            score += sum(w in (text or "") for w in words)
        if score:
            scores[path] = score
    ranked = sorted(scores, key=lambda p: (-scores[p], p))
    shown, more = [], []
    total = 0
    for path in ranked:
        if len(shown) >= MAX_MATCHES or total >= MATCH_CHARS:
            more.append(path)
            continue
        text = _read(folder / path, min(FILE_CHARS, MATCH_CHARS - total))
        if text is None:
            continue
        total += len(text)
        shown.append(f'<file path="{path}">\n{text}\n</file>')
    if not shown:
        return ""
    out = "## Files that match the task\n" + "\n\n".join(shown)
    if more:
        out += "\n\nAlso matching, not shown: " + ", ".join(more[:MAX_MORE])
    return out
