import json

import pytest

from claude_backlog import pack
from conftest import git

WORKFLOW = """\
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e ".[dev]"
      - name: Tests
        run: |
          ruff check src
          pytest -q
"""


@pytest.fixture(autouse=True)
def fresh_cache():
    pack.clear_cache()
    yield
    pack.clear_cache()


@pytest.fixture
def project(repo):
    (repo / "README.md").write_text("# Importer\n\nReads CSV files.\n")
    (repo / "CLAUDE.md").write_text("Use ASCII punctuation only.\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "importer"\n\n'
                                         '[project.scripts]\nimporter = "app.cli:main"\n')
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest",
                                                               "build": "vite build"}}))
    (repo / "Makefile").write_text("VAR := 1\n.PHONY: lint\nlint:\n\truff check\n"
                                   "test: lint\n\tpytest\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW)
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "src" / "app" / "importer.py").write_text("def parse_rows(text):\n"
                                                      "    return text.split()\n")
    (repo / "src" / "app" / "export.py").write_text("def write(rows):\n    pass\n")
    (repo / "src" / "app" / "slow.py").write_text("# the importer calls this\n")
    (repo / "uv.lock").write_text("parse_rows importer\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "project")
    return repo


def test_pack_holds_the_project(project):
    text = pack.build(project, "Make parse_rows faster")
    assert "## Files (10)" in text
    assert "src/app/importer.py\n" in text
    assert "## README.md\n# Importer" in text
    assert "## CLAUDE.md\nUse ASCII punctuation only." in text
    assert '[project.scripts]\nimporter = "app.cli:main"' in text
    assert "npm run test: jest" in text and "npm run build: vite build" in text
    assert "Makefile targets: make lint, make test" in text
    assert ("ci.yml run steps:\n  pip install -e \".[dev]\"\n  ruff check src\n"
            "  pytest -q") in text


def test_pack_holds_the_files_that_match_the_description(project):
    text = pack.build(project, "Make parse_rows faster; see export.py too.")
    matches = text.split("## Files that match the task\n", 1)[1]
    # Named files come first, then files with the key words.
    assert matches.startswith('<file path="src/app/export.py">\ndef write(rows):')
    assert '<file path="src/app/importer.py">\ndef parse_rows(text):' in matches
    assert "slow.py" not in matches
    assert "uv.lock" not in matches  # lock files are never shown
    text = pack.build(project, "Write the documentation for the importer")
    matches = text.split("## Files that match the task\n", 1)[1]
    assert "src/app/importer.py" in matches and "src/app/slow.py" in matches
    assert "## Files that match the task" not in pack.build(project, "Fix it all")


def test_keywords():
    assert pack.keywords("Make the import_rows step faster, and FASTER") == [
        "import_rows", "step", "faster"]


def test_pack_caps_sizes(project, monkeypatch):
    for i in range(5):
        (project / "src" / f"big{i}.py").write_text("needle\n" + "x" * 2000 + "\n")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "big")
    monkeypatch.setattr(pack, "MAX_FILES", 3)
    monkeypatch.setattr(pack, "FILE_CHARS", 500)
    monkeypatch.setattr(pack, "MATCH_CHARS", 1500)
    monkeypatch.setattr(pack, "DOC_CHARS", 10)
    text = pack.build(project, "Find the needle")
    assert "## Files (the first 3 of 15)" in text
    assert "## README.md\n# Importer\n[... cut after 10 characters]" in text
    matches = text.split("## Files that match the task\n", 1)[1]
    assert matches.count("<file path=") == 3
    assert "[... cut after 500 characters]" in matches
    assert "Also matching, not shown: src/big3.py, src/big4.py" in matches
    assert len(matches) < 1500 + 300


def test_cache_is_reused_for_the_same_head(project):
    assert "Reads CSV files." in pack.build(project, "x")
    (project / "README.md").write_text("# Importer\n\nChanged, not committed.\n")
    assert "Reads CSV files." in pack.build(project, "x")
    git(project, "commit", "-q", "-am", "new readme")
    text = pack.build(project, "x")
    assert "Changed, not committed." in text
    assert "Reads CSV files." not in text


def test_pack_of_a_folder_outside_git(plain_dir):
    (plain_dir / "notes.txt").write_text("ideas about the parser\n")
    (plain_dir / "node_modules" / "lib").mkdir(parents=True)
    (plain_dir / "node_modules" / "lib" / "index.js").write_text("parser\n")
    (plain_dir / "sub").mkdir()
    (plain_dir / "sub" / "parser.py").write_text("print(1)\n")
    text = pack.build(plain_dir, "Improve the parser")
    assert "## Files (2)\nnotes.txt\nsub/parser.py" in text
    assert "node_modules" not in text
    assert '<file path="sub/parser.py">' in text
    (plain_dir / "README.md").write_text("Not cached outside git.\n")
    assert "Not cached outside git." in pack.build(plain_dir, "x")


def test_binary_files_are_not_matched(project):
    (project / "importer.png").write_bytes(b"\x89PNG\0\0importer")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "image")
    assert "importer.png" not in pack.build(project, "Speed up the importer").split(
        "## Files that match the task")[1]
