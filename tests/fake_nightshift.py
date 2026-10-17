"""A stand-in for claude-nightshift in tests.

Each call takes the next step from the JSON plan in $FAKE_NS_PLAN and records
its arguments and working directory in $FAKE_NS_CALLS.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    args = sys.argv[1:]
    if "--usage-json" in args:
        print(json.dumps({"weekly_pct": 12.0, "weekly_resets": None, "five_hour_pct": 3.0,
                          "five_hour_resets": None, "source": "fake", "observed_at": 1.0}))
        return 0
    calls = Path(os.environ["FAKE_NS_CALLS"])
    with calls.open("a") as f:
        f.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\n")
    plan_path = Path(os.environ["FAKE_NS_PLAN"])
    plan = json.loads(plan_path.read_text())
    step = plan.pop(0) if plan else {"exit": 0, "summary": True}
    plan_path.write_text(json.dumps(plan))

    task_dir = Path(args[args.index("-f") + 1]).parent
    log = Path(args[args.index("--log-file") + 1])
    log.write_text("".join(f"2026-10-18 02:{i:02d}:00 - INFO - step {i} of the fake run\n"
                           for i in range(12)))
    if step.get("commit"):
        Path("work.txt").write_text(f"work at {time.time()}\n")
        subprocess.run(["git", "add", "work.txt"], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit",
                        "-q", "-m", "fake work"], check=True)
    if step.get("dirty"):
        Path("left-over.txt").write_text("not committed\n")
    if step.get("summary"):
        (task_dir / "SUMMARY.md").write_text(
            "# Summary\n\nDone.\n\n## Decisions for me\n\n- one\n- two\n")
    if step.get("sleep"):
        signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
        for _ in range(int(step.get("sleep_seconds", 3) * 20)):
            time.sleep(0.05)
        return 1
    return step.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
