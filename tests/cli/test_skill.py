"""Tests for `mship skill` CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("requested", ["omp", "pi"])
def test_skill_install_omp_and_pi_emit_one_canonical_result(
    tmp_path, monkeypatch, requested: str
):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = runner.invoke(
        app, ["skill", "install", "--only", requested, "--yes"]
    )

    assert result.exit_code == 0, result.output
    installed = json.loads(result.output)["installed"]
    assert [entry["agent"] for entry in installed] == ["omp"]
    assert (home / ".agents" / "skills" / "mothership").is_symlink()


def test_skill_install_codex_and_omp_share_one_target_without_duplication(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = runner.invoke(
        app, ["skill", "install", "--only", "codex,omp", "--yes"]
    )

    assert result.exit_code == 0, result.output
    installed = json.loads(result.output)["installed"]
    assert [entry["agent"] for entry in installed] == ["codex", "omp"]
    assert {entry["dest"] for entry in installed} == {
        str(home / ".agents" / "skills")
    }
    assert len(list((home / ".agents" / "skills").iterdir())) == 1


def test_skill_install_rejects_unknown_agent():
    result = runner.invoke(
        app, ["skill", "install", "--only", "unknown", "--yes"]
    )

    assert result.exit_code != 0
    assert "Unknown agent(s): unknown" in result.output


@pytest.mark.parametrize("executable", ["omp", "pi"])
def test_skill_install_auto_detects_omp_executables(
    tmp_path, monkeypatch, executable: str
):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "mship.core.skill_install.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == executable else None,
    )

    result = runner.invoke(app, ["skill", "install", "--yes"])

    assert result.exit_code == 0, result.output
    installed = json.loads(result.output)["installed"]
    assert [entry["agent"] for entry in installed] == ["omp"]


def test_skill_install_auto_detects_omp_config(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    (home / ".omp").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "mship.core.skill_install.shutil.which", lambda name: None
    )

    result = runner.invoke(app, ["skill", "install", "--yes"])

    assert result.exit_code == 0, result.output
    installed = json.loads(result.output)["installed"]
    assert [entry["agent"] for entry in installed] == ["omp"]


def test_skill_install_help_names_omp_and_pi_alias():
    result = runner.invoke(
        app, ["skill", "install", "--help"], terminal_width=120
    )

    assert result.exit_code == 0, result.output
    help_text = " ".join(result.output.lower().split())
    assert "pi" in help_text
    assert "alias for omp" in help_text


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
