"""Unit tests for mship skill discovery + install."""
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.cli import skill as skill_mod


runner = CliRunner()


# Sample tree returned by GitHub's `git/trees/<branch>?recursive=1` API
_FAKE_TREE = [
    {"path": "skills", "type": "tree"},
    {"path": "skills/working-with-mothership", "type": "tree"},
    {"path": "skills/working-with-mothership/SKILL.md", "type": "blob"},
    {"path": "skills/brainstorming", "type": "tree"},
    {"path": "skills/brainstorming/SKILL.md", "type": "blob"},
    {"path": "skills/brainstorming/visual-companion.md", "type": "blob"},
    {"path": "skills/brainstorming/references/themes.md", "type": "blob"},
    {"path": "skills/systematic-debugging", "type": "tree"},
    {"path": "skills/systematic-debugging/SKILL.md", "type": "blob"},
    # Skill dir with no SKILL.md should be excluded
    {"path": "skills/draft-idea", "type": "tree"},
    {"path": "skills/draft-idea/notes.md", "type": "blob"},
    # Non-skills content should be ignored
    {"path": "README.md", "type": "blob"},
    {"path": "src/mship/__init__.py", "type": "blob"},
]


def test_available_skills_filters_to_dirs_with_skill_md():
    skills = skill_mod._available_skills(_FAKE_TREE)
    assert skills == ["brainstorming", "systematic-debugging", "working-with-mothership"]


def test_available_skills_ignores_nested_skill_md():
    """A SKILL.md deeper than skills/<name>/SKILL.md shouldn't count."""
    tree = [
        {"path": "skills/foo/SKILL.md", "type": "blob"},
        {"path": "skills/foo/nested/SKILL.md", "type": "blob"},  # ignored
    ]
    assert skill_mod._available_skills(tree) == ["foo"]


def test_skill_files_returns_all_under_skill_prefix_sorted():
    files = skill_mod._skill_files(_FAKE_TREE, "brainstorming")
    assert files == [
        "skills/brainstorming/SKILL.md",
        "skills/brainstorming/references/themes.md",
        "skills/brainstorming/visual-companion.md",
    ]


def test_skill_files_empty_for_unknown_skill():
    assert skill_mod._skill_files(_FAKE_TREE, "does-not-exist") == []


def test_install_one_skill_writes_files_preserving_subdirs(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_mod, "_fetch_tree", lambda output: _FAKE_TREE)

    def fake_blob(path: str, output):
        return f"# content of {path}\n".encode()
    monkeypatch.setattr(skill_mod, "_fetch_blob", fake_blob)

    result = runner.invoke(
        app, ["skill", "install", "brainstorming", "--dest", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    root = tmp_path / "brainstorming"
    assert (root / "SKILL.md").read_text() == "# content of skills/brainstorming/SKILL.md\n"
    assert (root / "visual-companion.md").exists()
    assert (root / "references" / "themes.md").exists()  # subdir preserved


def test_install_all_installs_every_discovered_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_mod, "_fetch_tree", lambda output: _FAKE_TREE)
    monkeypatch.setattr(skill_mod, "_fetch_blob", lambda path, output: b"x")

    result = runner.invoke(app, ["skill", "install", "--all", "--dest", str(tmp_path)])
    assert result.exit_code == 0, result.output

    for name in ("working-with-mothership", "brainstorming", "systematic-debugging"):
        assert (tmp_path / name / "SKILL.md").exists(), name


def test_install_rejects_both_name_and_all(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_mod, "_fetch_tree", lambda output: _FAKE_TREE)
    result = runner.invoke(
        app, ["skill", "install", "brainstorming", "--all", "--dest", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "either" in result.output.lower() or "not both" in result.output.lower()


def test_install_rejects_unknown_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_mod, "_fetch_tree", lambda output: _FAKE_TREE)
    result = runner.invoke(
        app, ["skill", "install", "nonexistent", "--dest", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "nonexistent" in result.output
    assert "Available" in result.output


def test_list_reads_remote_tree(monkeypatch):
    monkeypatch.setattr(skill_mod, "_fetch_tree", lambda output: _FAKE_TREE)
    result = runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0, result.output
    # Expect all three discovered skills in output
    for name in ("working-with-mothership", "brainstorming", "systematic-debugging"):
        assert name in result.output
