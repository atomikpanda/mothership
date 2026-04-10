"""Integration test: mship init → mship status works end-to-end."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app, container
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


def test_init_then_status(tmp_path: Path, monkeypatch):
    """init creates valid config that status can load."""
    for name in ["shared", "api"]:
        d = tmp_path / name
        d.mkdir()
        (d / ".git").mkdir()
        (d / "Taskfile.yml").write_text("version: '3'\ntasks:\n  test:\n    cmds:\n      - echo ok\n")

    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [
        "init",
        "--name", "test-platform",
        "--repo", "./shared:library",
        "--repo", "./api:service:shared",
    ])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "mothership.yaml").exists()

    with open(tmp_path / "mothership.yaml") as f:
        data = yaml.safe_load(f)
    assert data["workspace"] == "test-platform"
    assert data["repos"]["api"]["depends_on"] == ["shared"]

    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No active task" in result.output

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared" in result.output
    assert "api" in result.output

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_init_detect_then_status(tmp_path: Path, monkeypatch):
    """init --detect finds repos and creates valid config."""
    for name in ["frontend", "backend"]:
        d = tmp_path / name
        d.mkdir()
        (d / ".git").mkdir()
        (d / "Taskfile.yml").write_text("version: '3'\ntasks:\n  test:\n    cmds:\n      - echo ok\n")

    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [
        "init",
        "--name", "my-app",
        "--detect",
    ])
    assert result.exit_code == 0, result.output

    with open(tmp_path / "mothership.yaml") as f:
        data = yaml.safe_load(f)
    assert "frontend" in data["repos"]
    assert "backend" in data["repos"]
