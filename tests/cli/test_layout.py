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


def test_review_tab_has_journal_pane():
    """The Review tab must include a Shell pane and a Journal pane wired to
    `mship view journal --watch`."""
    # Find the Review tab block.
    assert 'tab name="Review"' in _TEMPLATE
    start = _TEMPLATE.index('tab name="Review"')
    # The Run tab starts with `tab name="Run"`; slice up to it.
    end = _TEMPLATE.index('tab name="Run"', start)
    review_block = _TEMPLATE[start:end]

    assert 'name="Shell"' in review_block, review_block
    assert 'name="Journal"' in review_block, review_block
    assert '"view" "journal" "--watch"' in review_block, review_block
