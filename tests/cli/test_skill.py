"""Tests for `mship skill` CLI."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app


runner = CliRunner()


def test_skill_list_returns_package_skill_names():
    result = runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "skills" in data
    assert "working-with-mothership" in data["skills"]


def test_skill_install_for_claude_creates_user_scope_symlinks(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "mship.core.skill_install._detect_agents",
        lambda: {"claude": True, "codex": False, "gemini": False},
    )
    result = runner.invoke(app, ["skill", "install"])
    assert result.exit_code == 0, result.output
    target = home / ".claude" / "skills" / "working-with-mothership" / "SKILL.md"
    assert target.exists(), f"missing: {target}"


def test_skill_install_only_flag_limits_agents(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "mship.core.skill_install._detect_agents",
        lambda: {"claude": True, "codex": True, "gemini": False},
    )
    result = runner.invoke(app, ["skill", "install", "--only", "codex"])
    assert result.exit_code == 0, result.output
    assert (home / ".agents" / "skills" / "mothership").is_symlink()
    assert not (home / ".claude" / "skills").exists()


def test_skill_install_warns_about_legacy_codex_mothership_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    legacy = home / ".codex" / "mothership"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "mship.core.skill_install._detect_agents",
        lambda: {"claude": False, "codex": True, "gemini": False},
    )
    result = runner.invoke(app, ["skill", "install"])
    assert result.exit_code == 0, result.output
    assert "no longer used" in result.output
    assert ".codex/mothership" in result.output
