"""Stands in for `claude -p` when drafting a prompt.

FAKE_CLAUDE_MODE: "ok" (default), "blocking" (one question blocks the
task), "error" (an error result), "garbage" (output that is not JSON).
FAKE_CLAUDE_ARGS: a file to write the arguments, the working folder and
the request (standard input) to, as JSON.
"""

import json
import os
import sys

args = sys.argv[1:]
request = sys.stdin.read()
record = os.environ.get("FAKE_CLAUDE_ARGS")
if record:
    with open(record, "w") as f:
        json.dump({"args": args, "cwd": os.getcwd(), "request": request}, f)
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "garbage":
    print("something went wrong", file=sys.stderr)
    sys.exit(1)
if mode == "error":
    print(json.dumps({"is_error": True, "subtype": "error", "result": "usage limit"}))
    sys.exit(0)
questions = []
if "<description>" in request:
    questions = [
        {"question": "Keep the old format?", "assumption": "Yes, it stays.",
         "blocking": False},
        {"question": " ", "assumption": "", "blocking": False},
        "not a question",
        *({"question": f"Q{i}", "assumption": f"A{i}", "blocking": False}
          for i in range(2, 7)),
    ]
if mode == "blocking":
    questions = [{"question": "Which database?", "assumption": "SQLite.",
                  "blocking": True}]
print(json.dumps({
    "is_error": False,
    "subtype": "success",
    "structured_output": {
        "prompt": f"## Goal\nDone in {os.getcwd()}.\n",
        "title": "Speed up imports",
        "questions": questions,
    },
}))
