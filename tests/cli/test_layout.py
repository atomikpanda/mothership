from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.cli.layout import _TEMPLATE

runner = CliRunner()


def _expected_path(tmp_path: Path) -> Path:
    return tmp_path / ".config" / "zellij" / "layouts" / "mothership.kdl"


def test_layout_init_writes_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["layout", "init"])
    assert result.exit_code == 0, result.output
    target = _expected_path(tmp_path)
    assert target.exists()
    assert target.read_text() == _TEMPLATE


def test_layout_init_refuses_when_exists_without_force(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = _expected_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    original_content = "original content"
    target.write_text(original_content)

    result = runner.invoke(app, ["layout", "init"])
    assert result.exit_code == 1
    assert target.read_text() == original_content


def test_layout_init_overwrites_with_force(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = _expected_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old content")

    result = runner.invoke(app, ["layout", "init", "--force"])
    assert result.exit_code == 0, result.output
    assert target.read_text() == _TEMPLATE


def test_layout_launch_execs_zellij(tmp_path: Path, monkeypatch):
    captured = {}

    def fake_execvp(file, args):
        captured["file"] = file
        captured["args"] = args

    monkeypatch.setattr("os.execvp", fake_execvp)
    result = runner.invoke(app, ["layout", "launch"])
    assert result.exit_code == 0, result.output
    assert captured["file"] == "zellij"
    assert captured["args"] == ["zellij", "--layout", "mothership"]
