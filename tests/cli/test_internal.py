"""Tests for hidden _check-commit / _post-checkout / _journal-commit commands."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container


runner = CliRunner()


def test_get_container_required_false_returns_none_when_no_workspace(tmp_path, monkeypatch, capsys):
    """Outside any workspace, get_container(required=False) must be silent.
    See #86."""
    from mship.cli import get_container
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    result = get_container(required=False)
    captured = capsys.readouterr()
    assert result is None
    assert captured.err == ""  # no "No mothership.yaml found" noise
    assert captured.out == ""


def test_get_container_required_true_still_errors_loudly(tmp_path, monkeypatch, capsys):
    """Regression: default behavior unchanged — prints + raises."""
    import typer
    from mship.cli import get_container
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    with pytest.raises(typer.Exit) as exc:
        get_container()  # required=True by default
    captured = capsys.readouterr()
    assert exc.value.exit_code == 1
    assert "No mothership.yaml" in captured.err


def test_check_commit_silent_outside_workspace(tmp_path, monkeypatch):
    """_check-commit in a dir with no workspace ancestor exits 0 silently.
    See #86."""
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    result = runner.invoke(app, ["_check-commit", str(tmp_path)])
    assert result.exit_code == 0
    assert "No mothership.yaml" not in (result.output or "")


def test_journal_commit_silent_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["_journal-commit"])
    assert result.exit_code == 0
    assert "No mothership.yaml" not in (result.output or "")
