from claude_backlog.prompt import build_prompt, decisions_count


def task(**extra):
    base = {"project_dir": "/p/app", "prompt": "Do it.\n", "worktree": None,
            "branch": None, "base_branch": None, "base_commit": None}
    return {**base, **extra}


def test_prompt_in_a_worktree_mentions_the_branch():
    text = build_prompt(task(worktree="/w/x", branch="backlog/x-1", base_branch="main",
                             base_commit="abcdef1234567890"), "/data/tasks/x-1")
    assert text.startswith("This is an unattended run.")
    assert "/data/tasks/x-1/STATE.md" in text
    assert "on the branch backlog/x-1" in text
    assert "from main at abcdef123456" in text
    assert "Do not commit" not in text
    assert text.endswith("The task:\n\nDo it.\n")


def test_prompt_without_git_forbids_commits():
    text = build_prompt(task(), "/data/tasks/x-1")
    assert "Only change files inside /p/app and /data/tasks/x-1" in text
    assert "Do not commit" in text
    assert "worktree" not in text


def test_decisions_count():
    summary = """# Summary
- not a decision

## Decisions for me

- keep the old API?
* rename the module
1. which license
2) when to release

## Problems
- something
"""
    assert decisions_count(summary) == 4
    assert decisions_count("# Summary\nAll done.") == 0
