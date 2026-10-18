"""Stands in for `claude -p` when drafting a prompt.

FAKE_CLAUDE_MODE: "ok" (default), "error" (an error result), "garbage"
(output that is not JSON).
"""

import json
import os
import sys

args = sys.argv[1:]
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "garbage":
    print("something went wrong", file=sys.stderr)
    sys.exit(1)
if mode == "error":
    print(json.dumps({"is_error": True, "subtype": "error", "result": "usage limit"}))
    sys.exit(0)
tools = args[args.index("--tools") + 1]
request = args[args.index("-p") + 1]
print(json.dumps({
    "is_error": False,
    "subtype": "success",
    "structured_output": {
        "prompt": f"## Goal\nDone in {os.getcwd()} with {tools}.\n",
        "title": "Speed up imports",
        "questions": ["Keep the old format?", " ", "Q2", "Q3", "Q4", "Q5", "Q6"]
        if "<description>" in request else [],
    },
}))
