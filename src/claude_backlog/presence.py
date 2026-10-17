"""Away detection: is the screensaver on, and for how long?

"Away" means the screensaver has been on for a set number of minutes. Each
desktop exposes that differently, so there is one small detector per desktop.
Adding a desktop means adding a detector to :data:`DETECTORS`.
"""

from __future__ import annotations

import getpass
import logging
import re
import shutil
import subprocess
import threading
import time

from claude_backlog import db, settings, tasks

__all__ = [
    "Detector",
    "ScreenSaverDetector",
    "LogindDetector",
    "DETECTORS",
    "find_detector",
    "Watcher",
]

logger = logging.getLogger(__name__)

POLL_SECONDS = 30


def _run(cmd, timeout=5) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class Detector:
    name = "none"

    def away_seconds(self) -> int | None:
        """Seconds the screensaver has been on: 0 when off, None when unknown."""
        raise NotImplementedError


class ScreenSaverDetector(Detector):
    """A screensaver with a ``GetActiveTime`` method on the session D-Bus."""

    def __init__(self, name, dest, path, interface):
        self.name = name
        self.dest = dest
        self.path = path
        self.interface = interface

    def away_seconds(self):
        if not shutil.which("gdbus"):
            return None
        out = _run(["gdbus", "call", "--session", "--dest", self.dest,
                    "--object-path", self.path,
                    "--method", f"{self.interface}.GetActiveTime"])
        if out is None:
            return None
        # gdbus prints "(uint32 754,)"; the value is the last number.
        numbers = re.findall(r"\d+", out)
        return int(numbers[-1]) if numbers else None


class LogindDetector(Detector):
    """Falls back on logind's LockedHint/IdleHint, which carry no duration.

    Away is counted from the first poll that saw the hint set.
    """

    name = "logind"

    def __init__(self, clock=time.time):
        self.clock = clock
        self.since = None

    def _session(self):
        return _run(["loginctl", "show-user", getpass.getuser(), "-p", "Display",
                     "--value"])

    def away_seconds(self):
        if not shutil.which("loginctl"):
            return None
        session = self._session()
        if not session:
            return None
        out = _run(["loginctl", "show-session", session, "-p", "LockedHint",
                    "-p", "IdleHint", "--value"])
        if out is None:
            return None
        hinted = "yes" in out.split()
        if not hinted:
            self.since = None
            return 0
        if self.since is None:
            self.since = self.clock()
        return int(self.clock() - self.since)


DETECTORS = [
    ScreenSaverDetector("Cinnamon screensaver", "org.cinnamon.ScreenSaver",
                        "/org/cinnamon/ScreenSaver", "org.cinnamon.ScreenSaver"),
    ScreenSaverDetector("GNOME screensaver", "org.gnome.ScreenSaver",
                        "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver"),
    ScreenSaverDetector("freedesktop screensaver", "org.freedesktop.ScreenSaver",
                        "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver"),
    LogindDetector(),
]


def find_detector(candidates=None) -> Detector | None:
    """The first detector that gives an answer on this desktop."""
    for detector in candidates if candidates is not None else DETECTORS:
        if detector.away_seconds() is not None:
            return detector
    return None


class Watcher:
    """Starts the runner when I am away and stops it when I am back."""

    def __init__(self, runner, db_path=None, detector=None, find=find_detector,
                 on_change=None):
        self.runner = runner
        self.db_path = db_path
        self.detector = detector
        self.find = find
        self.on_change = on_change or (lambda: None)
        self.away = None  # last reading, seconds
        self.read_at = None
        self.started_this_time = False
        self._stop = threading.Event()
        self._thread = None

    def status(self) -> dict:
        return {
            "detector": self.detector.name if self.detector else None,
            "away_seconds": self.away,
            "read_at": self.read_at,
        }

    def tick(self, conn) -> None:
        if self.detector is None:
            self.detector = self.find()
        before = self.away
        away = self.detector.away_seconds() if self.detector else None
        if away is None and self.detector is not None:
            self.detector = self.find()  # the desktop changed, or a service died
            away = self.detector.away_seconds() if self.detector else None
        self.away = away
        self.read_at = time.time()
        if (before is None) != (away is None) or bool(before) != bool(away):
            self.on_change()
        if away is None:
            return

        conf = settings.get_all(conn)
        status = self.runner.status()
        if away == 0:
            self.started_this_time = False
            if status["trigger"] == "away" and status["state"] == "running":
                now = conf["on_return"] == "stop"
                logger.info("you are back; stopping the run %s",
                            "now" if now else "after this task")
                self.runner.stop(now=now, cause="back")
            return

        if (conf["away_enabled"] and away >= conf["away_minutes"] * 60
                and status["state"] == "idle" and not self.started_this_time
                and tasks.next_queued(conn) is not None):
            logger.info("away for %ss; starting the queue", away)
            self.started_this_time = True
            self.runner.start(trigger="away")

    def _loop(self) -> None:
        conn = db.connect(self.db_path)
        try:
            while not self._stop.is_set():
                try:
                    self.tick(conn)
                except Exception:
                    logger.exception("away detection failed")
                self._stop.wait(POLL_SECONDS)
        finally:
            conn.close()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="backlog-presence",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
