"""The parts that depend on the operating system: keeping awake and notifying.

Away detection lives in :mod:`claude_backlog.presence`. Each part is small so
another platform can be added next to the Linux one.
"""

from __future__ import annotations

import shutil
import subprocess

__all__ = ["Inhibitor", "notify"]


class Inhibitor:
    """Hold a systemd-inhibit lock against sleep and idle while the queue runs."""

    def __init__(self):
        self.proc = None
        self.available = shutil.which("systemd-inhibit") is not None

    def start(self, why: str) -> bool:
        if self.proc or not self.available:
            return False
        self.proc = subprocess.Popen(
            ["systemd-inhibit", "--what=sleep:idle", "--who=claude-backlog",
             f"--why={why}", "--mode=block", "sleep", "infinity"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


def notify(title: str, body: str) -> None:
    """A desktop notification, when notify-send is there."""
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "--app-name=claude-backlog", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
