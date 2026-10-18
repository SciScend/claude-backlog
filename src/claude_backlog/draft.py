"""Write a task's prompt with Claude, from a description in the user's words.

One ``claude -p`` call with no tools: the project pack (pack.py) goes into
the request, and the answer is the prompt, a title and the questions the
description leaves open, each with the assumption the prompt makes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from claude_backlog import pack

__all__ = ["CLAUDE_ENV", "DraftError", "build_request", "command", "draft"]

# Overrides the claude command (for tests and unusual installs).
CLAUDE_ENV = "CLAUDE_BACKLOG_CLAUDE"

TIMEOUT = 180
MAX_QUESTIONS = 5

SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "title": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "assumption": {"type": "string"},
                    "blocking": {"type": "boolean"},
                },
                "required": ["question", "assumption", "blocking"],
            },
        },
    },
    "required": ["prompt", "title", "questions"],
}

REQUEST = """\
Write the prompt for a coding task that another Claude Code session will do
later, unattended, in the project folder {project_dir}. Nobody will be
there to answer its questions, so the prompt is all it will know. Do not do
the task yourself. You have no tools: what you know about the project is in
the project pack below.

What the user wants, in their words:

<description>
{description}
</description>

The project pack: the file list, the start of README and of CLAUDE.md or
AGENTS.md, the build and test commands, and the files that match the
description (long ones are cut).

<project>
{pack}
</project>

How to write it:

1. Use only paths, names and commands that appear in the project pack; do
   not guess. Where the task needs something the pack does not show, tell
   the session where to look for it instead.
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
   will notice), the default is to assume and go on: make the most
   reasonable assumption and write it into the prompt. List each such
   choice in "questions": at most {max_questions}, only ones that matter, each
   answerable in a sentence, with "assumption" saying what the prompt
   assumes. Set "blocking" to true only when the task cannot sensibly be
   done without the answer: the goals contradict each other, or it is a
   choice between incompatible designs where a wrong guess wastes the
   whole run. Everything else is not blocking. No questions if nothing
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


def build_request(description: str, template: str, project_dir: str,
                  project_pack: str) -> str:
    template = template.strip() or "(no template: use Goal, Context, Done when)"
    return REQUEST.format(description=description.strip(), template=template,
                          project_dir=project_dir, pack=project_pack.strip(),
                          max_questions=MAX_QUESTIONS)


def command(model: str = "", effort: str = "") -> list[str]:
    """The claude command line; the request goes to its standard input.

    No tools and no MCP servers: everything Claude needs is in the request.
    The model and effort are always passed when set, so the user's own
    Claude Code defaults do not decide them.
    """
    cmd = [*claude_command(), "-p", "--tools", "", "--strict-mcp-config",
           "--no-session-persistence", "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA)]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    return cmd


def _question(item) -> dict | None:
    if not isinstance(item, dict) or not str(item.get("question") or "").strip():
        return None
    return {"question": str(item["question"]).strip(),
            "assumption": str(item.get("assumption") or "").strip(),
            "blocking": item.get("blocking") is True}


def draft(description: str, project_dir: str, template: str, model: str = "",
          effort: str = "") -> dict:
    """Returns {"prompt", "title", "questions"}; raises DraftError.

    Each question is {"question", "assumption", "blocking"}.
    """
    if not description.strip():
        raise DraftError("Describe the task first.")
    folder = Path(project_dir).expanduser()
    if not folder.is_dir():
        raise DraftError(f"The project folder does not exist: {project_dir}")
    request = build_request(description, template, str(folder),
                            pack.build(folder, description))
    try:
        result = subprocess.run(command(model, effort), cwd=folder, input=request,
                                capture_output=True, text=True, timeout=TIMEOUT)
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
    if not isinstance(data, dict):
        data = {}
    out = data.get("structured_output")
    if data.get("is_error") or not isinstance(out, dict) or not out.get("prompt"):
        raise DraftError("Claude failed: " + str(data.get("result") or data.get("subtype")
                                                 or "no prompt in the answer"))
    questions = [q for q in map(_question, out.get("questions") or []) if q]
    return {"prompt": out["prompt"].strip() + "\n",
            "title": (out.get("title") or "").strip(),
            "questions": questions[:MAX_QUESTIONS]}
