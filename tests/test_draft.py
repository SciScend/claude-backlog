import sys
from pathlib import Path

import pytest

from claude_backlog import draft, settings

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture(autouse=True)
def fake_claude(monkeypatch):
    monkeypatch.setenv(draft.CLAUDE_ENV, f"{sys.executable} {FAKE}")


def test_request_holds_the_description_and_the_template():
    text = draft.build_request("Make imports fast", settings.PROMPT_TEMPLATE, "/p/app")
    assert "<description>\nMake imports fast\n</description>" in text
    assert "## When unsure" in text
    assert "/p/app" in text


def test_draft_runs_read_only_in_the_project(plain_dir):
    result = draft.draft("Make imports fast", str(plain_dir), settings.PROMPT_TEMPLATE)
    assert result["prompt"] == f"## Goal\nDone in {plain_dir} with Read,Grep,Glob.\n"
    assert result["title"] == "Speed up imports"
    assert result["questions"] == ["Keep the old format?", "Q2", "Q3", "Q4", "Q5"]


def test_draft_errors(plain_dir, monkeypatch):
    with pytest.raises(draft.DraftError, match="Describe"):
        draft.draft("  ", str(plain_dir), "")
    with pytest.raises(draft.DraftError, match="does not exist"):
        draft.draft("x", str(plain_dir / "missing"), "")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    with pytest.raises(draft.DraftError, match="usage limit"):
        draft.draft("x", str(plain_dir), "")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    with pytest.raises(draft.DraftError, match="something went wrong"):
        draft.draft("x", str(plain_dir), "")
    monkeypatch.setenv(draft.CLAUDE_ENV, "/nonexistent/claude")
    with pytest.raises(draft.DraftError, match="not installed"):
        draft.draft("x", str(plain_dir), "")
