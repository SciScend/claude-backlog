import pytest

from claude_backlog import settings, tasks
from claude_backlog.presence import (
    Detector,
    LogindDetector,
    ScreenSaverDetector,
    Watcher,
    find_detector,
)


class FakeDetector(Detector):
    name = "fake"

    def __init__(self):
        self.value = 0

    def away_seconds(self):
        return self.value


class FakeRunner:
    def __init__(self):
        self.state = "idle"
        self.trigger = None
        self.starts = []
        self.stops = []

    def status(self):
        return {"state": self.state, "trigger": self.trigger}

    def start(self, trigger="run-now", until=None):
        self.starts.append(trigger)
        self.state = "running"
        self.trigger = trigger
        return True

    def stop(self, now=False, cause="you"):
        self.stops.append((now, cause))
        return True


@pytest.fixture
def setup(conn, plain_dir):
    detector = FakeDetector()
    runner = FakeRunner()
    watcher = Watcher(runner, detector=detector, find=lambda: detector)
    tasks.create(conn, "x", plain_dir)
    return detector, runner, watcher


def test_starts_after_the_away_delay(conn, setup):
    detector, runner, watcher = setup
    detector.value = 14 * 60
    watcher.tick(conn)
    assert runner.starts == []
    detector.value = 15 * 60
    watcher.tick(conn)
    assert runner.starts == ["away"]


def test_starts_only_once_per_absence(conn, setup):
    detector, runner, watcher = setup
    detector.value = 20 * 60
    watcher.tick(conn)
    runner.state = "idle"  # the queue ran empty or hit the limit
    detector.value = 30 * 60
    watcher.tick(conn)
    assert runner.starts == ["away"]
    detector.value = 0
    watcher.tick(conn)
    detector.value = 20 * 60
    watcher.tick(conn)
    assert runner.starts == ["away", "away"]


def test_does_not_start_when_switched_off_or_empty(conn, setup, plain_dir):
    detector, runner, watcher = setup
    settings.update(conn, {"away_enabled": False})
    detector.value = 60 * 60
    watcher.tick(conn)
    assert runner.starts == []
    settings.update(conn, {"away_enabled": True})
    for t in tasks.queue(conn):
        tasks.move(conn, t["id"], "hold")
    watcher.tick(conn)
    assert runner.starts == []


def test_stops_an_away_run_when_i_come_back(conn, setup):
    detector, runner, watcher = setup
    detector.value = 20 * 60
    watcher.tick(conn)
    detector.value = 0
    watcher.tick(conn)
    assert runner.stops == [(True, "back")]


def test_lets_the_task_finish_when_set_so(conn, setup):
    detector, runner, watcher = setup
    settings.update(conn, {"on_return": "finish"})
    detector.value = 20 * 60
    watcher.tick(conn)
    detector.value = 0
    watcher.tick(conn)
    assert runner.stops == [(False, "back")]


def test_does_not_stop_a_run_i_started(conn, setup):
    detector, runner, watcher = setup
    runner.start("run-now")
    detector.value = 0
    watcher.tick(conn)
    assert runner.stops == []


def test_unknown_reading_does_nothing(conn, setup):
    detector, runner, watcher = setup
    detector.value = None
    watcher.tick(conn)
    assert runner.starts == [] and runner.stops == []


def test_screensaver_detector_parses_gdbus(monkeypatch):
    from claude_backlog import presence

    monkeypatch.setattr(presence.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(presence, "_run", lambda cmd, timeout=5: "(uint32 754,)")
    d = ScreenSaverDetector("x", "org.x", "/org/x", "org.x")
    assert d.away_seconds() == 754
    monkeypatch.setattr(presence, "_run", lambda cmd, timeout=5: None)
    assert d.away_seconds() is None


def test_logind_detector_counts_from_the_first_hint(monkeypatch):
    from claude_backlog import presence

    now = [1000.0]
    answers = {"show-user": "c2", "show-session": "no\nno"}
    monkeypatch.setattr(presence.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(presence, "_run", lambda cmd, timeout=5: answers[cmd[1]])
    d = LogindDetector(clock=lambda: now[0])
    assert d.away_seconds() == 0
    answers["show-session"] = "yes\nno"
    assert d.away_seconds() == 0
    now[0] += 90
    assert d.away_seconds() == 90
    answers["show-session"] = "no\nno"
    assert d.away_seconds() == 0


def test_find_detector_takes_the_first_that_answers():
    class Silent(Detector):
        def away_seconds(self):
            return None

    working = FakeDetector()
    assert find_detector([Silent(), working]) is working
    assert find_detector([Silent()]) is None
