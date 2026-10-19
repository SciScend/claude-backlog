"""A stand-in for claude-nightshift in tests.

Each call takes the next step from the JSON plan in $FAKE_NS_PLAN and records
its arguments and working directory in $FAKE_NS_CALLS. A plan that is an
object keeps a list of steps per task id, for tasks that run at the same time.
"""

import fcntl
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
        print(json.dumps({"weekly_pct": 12.0, "weekly_resets": 1792400400.0,
                          "five_hour_pct": 3.0, "five_hour_resets": 1792243200.0,
                          "source": "fake", "observed_at": 1.0}))
        return 0
    if "--runs-json" in args:
        runs = Path(os.environ["FAKE_NS_PLAN"]).with_name("runs.json")
        print(runs.read_text() if runs.exists() else "{}")
        return 0
    task_dir = Path(args[args.index("-f") + 1]).parent
    plan_path = Path(os.environ["FAKE_NS_PLAN"])
    with open(str(plan_path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        calls = Path(os.environ["FAKE_NS_CALLS"])
        with calls.open("a") as f:
            f.write(json.dumps({"args": args, "cwd": os.getcwd(),
                                "start": time.time()}) + "\n")
        plan = json.loads(plan_path.read_text())
        steps = plan.get(task_dir.name, []) if isinstance(plan, dict) else plan
        step = steps.pop(0) if steps else {"exit": 0, "summary": True}
        plan_path.write_text(json.dumps(plan))

    log = Path(args[args.index("--log-file") + 1])
    log.write_text("".join(f"2026-10-18 02:{i:02d}:00 - INFO - step {i} of the fake run\n"
                           for i in range(12)))
    if step.get("commit"):
        name = step.get("file", "work.txt")
        Path(name).write_text(step.get("text", f"work at {time.time()}\n"))
        subprocess.run(["git", "add", name], check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit",
                        "-q", "-m", "fake work"], check=True)
    for args in step.get("git", []):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)
    if step.get("dirty"):
        Path("left-over.txt").write_text("not committed\n")
    if step.get("summary"):
        (task_dir / "SUMMARY.md").write_text(
            "# Summary\n\nDone.\n\n## Decisions for me\n\n- one\n- two\n")
    if step.get("sleep"):
        signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
        for _ in range(int(step.get("sleep_seconds", 3) * 20)):
            time.sleep(0.05)
        if "exit" not in step:
            return 1
    with open(str(calls) + ".ends", "a") as f:
        f.write(json.dumps({"task": task_dir.name, "end": time.time()}) + "\n")
    return step.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
