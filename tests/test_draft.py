import json
import sys
from pathlib import Path

import pytest

from claude_backlog import db, draft, settings

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture(autouse=True)
def fake_claude(monkeypatch, tmp_path):
    monkeypatch.setenv(draft.CLAUDE_ENV, f"{sys.executable} {FAKE}")
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "claude-args.json"))


@pytest.fixture
def claude_call(tmp_path):
    """What the fake claude was called with: args, cwd and request."""
    return lambda: json.loads((tmp_path / "claude-args.json").read_text())


def test_request_holds_the_description_the_pack_and_the_template():
    text = draft.build_request("Make imports fast", settings.PROMPT_TEMPLATE, "/p/app",
                               "## Files (1)\nmain.py\n")
    assert "<description>\nMake imports fast\n</description>" in text
    assert "<project>\n## Files (1)\nmain.py\n</project>" in text
    assert "## When unsure" in text
    assert "/p/app" in text
    assert "the default is to assume and go on" in text


def test_command_has_no_tools_and_the_model_and_effort():
    cmd = draft.command("sonnet", "low")
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == draft.SCHEMA
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--effort") + 1] == "low"
    cmd = draft.command("", "")
    assert "--model" not in cmd and "--effort" not in cmd


def test_draft_is_one_call_with_the_pack(repo, claude_call):
    (repo / "importer.py").write_text("def load():\n    pass\n")
    result = draft.draft("Make imports in importer.py fast", str(repo),
                         settings.PROMPT_TEMPLATE, "haiku", "medium")
    assert result["prompt"] == f"## Goal\nDone in {repo}.\n"
    assert result["title"] == "Speed up imports"
    assert result["questions"] == [
        {"question": "Keep the old format?", "assumption": "Yes, it stays.",
         "blocking": False},
        *({"question": f"Q{i}", "assumption": f"A{i}", "blocking": False}
          for i in range(2, 6)),
    ]
    call = claude_call()
    assert call["cwd"] == str(repo)
    args = call["args"]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == "haiku"
    assert args[args.index("--effort") + 1] == "medium"
    assert "<description>\nMake imports in importer.py fast\n</description>" \
        in call["request"]
    assert "## README.md\nhello" in call["request"]


def test_draft_uses_the_draft_settings(repo, claude_call):
    conn = db.connect()
    conf = settings.get_all(conn)
    assert (conf["draft_model"], conf["draft_effort"]) == ("sonnet", "low")
    draft.draft("x", str(repo), "", conf["draft_model"], conf["draft_effort"])
    args = claude_call()["args"]
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--effort") + 1] == "low"
    conf = settings.update(conn, {"draft_model": "", "draft_effort": ""})
    draft.draft("x", str(repo), "", conf["draft_model"], conf["draft_effort"])
    args = claude_call()["args"]
    assert "--model" not in args and "--effort" not in args
    with pytest.raises(ValueError):
        settings.update(conn, {"draft_model": "gpt"})
    with pytest.raises(ValueError):
        settings.update(conn, {"draft_effort": "huge"})
    conn.close()


def test_blocking_questions(plain_dir, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "blocking")
    result = draft.draft("Store the tasks", str(plain_dir), "")
    assert result["questions"] == [{"question": "Which database?",
                                    "assumption": "SQLite.", "blocking": True}]


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
