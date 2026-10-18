"""Write a task's prompt with Claude, from a description in the user's words.

One short ``claude -p`` session in the project folder, with read-only tools,
that answers with the prompt and the questions it could not settle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

__all__ = ["CLAUDE_ENV", "DraftError", "build_request", "draft"]

# Overrides the claude command (for tests and unusual installs).
CLAUDE_ENV = "CLAUDE_BACKLOG_CLAUDE"

TIMEOUT = 600
MAX_QUESTIONS = 5

SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "title": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["prompt", "title", "questions"],
}

REQUEST = """\
Write the prompt for a coding task that another Claude Code session will do
later, unattended, in the project folder {project_dir}. Nobody will be
there to answer its questions, so the prompt is all it will know. Do not do
the task yourself and do not change anything: only read.

What the user wants, in their words:

<description>
{description}
</description>

How to write it:

1. Read the project first: the files the task is about, README and
   CLAUDE.md or AGENTS.md if there are any, and how the project is built,
   run and tested (package.json, pyproject.toml, Makefile, CI files).
   Use only what you have seen; do not guess paths or commands.
2. Follow this template. Replace every line in angle brackets with real
   content, or delete it; delete a whole section only if the template says
   it is optional and it does not apply. Keep the headings.

<template>
{template}
</template>

3. Goal: what should be true when the task is done, and why, in a few
   sentences. Context: concrete paths, the relevant functions, the code to
   follow as a pattern, and the exact commands to build, run and test.
   Done when: results someone can observe, each with the exact command or
   step that checks it; prefer the project's own tests, and ask for a new
   test that fails before the change when that fits. Milestones: for a task
   longer than an hour or so, steps in order that can each be checked on
   their own. When unsure: which choices are fine to make alone and which
   to leave for the user, and what to do if something blocks the task.
4. Stay within what the user asked. Where the description leaves a choice
   open that changes the result (scope, compatibility, behavior the user
   will notice), make the most reasonable assumption, write it into the
   prompt, and also ask about it in "questions": at most {max_questions}, only
   ones that matter, each answerable in a sentence. No questions if nothing
   important is open.
5. Leave out rules every task gets anyway: working without questions,
   STATE.md, SUMMARY.md, git branches and commits, not weakening tests.
6. Write in the language of the description. "title": a short imperative
   title, at most 60 characters, in the same language.
"""


class DraftError(Exception):
    pass


def claude_command() -> list[str]:
    override = os.environ.get(CLAUDE_ENV)
    if override:
        return override.split()
    return [shutil.which("claude") or "claude"]


def build_request(description: str, template: str, project_dir: str) -> str:
    template = template.strip() or "(no template: use Goal, Context, Done when)"
    return REQUEST.format(description=description.strip(), template=template,
                          project_dir=project_dir, max_questions=MAX_QUESTIONS)


def draft(description: str, project_dir: str, template: str, model: str = "") -> dict:
    """Returns {"prompt", "title", "questions"}; raises DraftError."""
    if not description.strip():
        raise DraftError("Describe the task first.")
    folder = Path(project_dir).expanduser()
    if not folder.is_dir():
        raise DraftError(f"The project folder does not exist: {project_dir}")
    cmd = [*claude_command(), "-p", build_request(description, template, str(folder)),
           "--tools", "Read,Grep,Glob", "--permission-mode", "dontAsk",
           "--no-session-persistence", "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA)]
    if model:
        cmd += ["--model", model]
    try:
        result = subprocess.run(cmd, cwd=folder, capture_output=True, text=True,
                                timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise DraftError("Claude Code ('claude') is not installed or not on PATH.") \
            from None
    except subprocess.TimeoutExpired:
        raise DraftError(f"Claude did not finish within {TIMEOUT // 60} minutes.") from None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise DraftError("Claude failed: " + (detail[-1] if detail else
                                               f"exit code {result.returncode}")) from None
    out = data.get("structured_output")
    if data.get("is_error") or not isinstance(out, dict) or not out.get("prompt"):
        raise DraftError("Claude failed: " + str(data.get("result") or data.get("subtype")
                                                 or "no prompt in the answer"))
    questions = [q.strip() for q in out.get("questions") or [] if str(q).strip()]
    return {"prompt": out["prompt"].strip() + "\n",
            "title": (out.get("title") or "").strip(),
            "questions": questions[:MAX_QUESTIONS]}
