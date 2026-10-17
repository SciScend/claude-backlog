# Contributing

Thank you for helping. Issues and pull requests are welcome.

## Setup

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src tests
```

The tests run without Claude, a Claude login, a desktop or systemd.
`tests/fake_nightshift.py` stands in for claude-nightshift and follows a
plan of exit codes and actions per call; `tests/conftest.py` gives every
test its own data folder and a throwaway git repository.

To try the board by hand without touching your real backlog:

```bash
CLAUDE_BACKLOG_HOME=/tmp/backlog-try uv run claude-backlog serve --port 18765
```

## Guidelines

- Keep the split with claude-nightshift: nightshift runs one task and owns
  everything about usage limits; claude-backlog never decides anything from
  usage figures. If backlog needs nightshift to behave differently, change
  nightshift.
- Use only ASCII punctuation in code, docs and commit messages.
- Nothing machine-specific in code, defaults, tests or examples.
- Every change comes with tests.
- Changes to behaviour go into `docs/design.md` first or together with the
  code.

## Adding a desktop to away detection

Away detection asks the desktop how long the screensaver has been on. Each
desktop is a small detector in `src/claude_backlog/presence.py`: a class with
an `away_seconds()` method that returns the seconds (0 when the screensaver is
off, `None` when it cannot tell). Add it to `DETECTORS` before the logind
fallback, with a test that fakes the command's output.

## Reporting a security problem

Please do not open a public issue. Write to the maintainer through the
contact on the SciScend GitHub organization page.
