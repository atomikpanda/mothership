"""CLI-level regression tests for view watch-mode tolerance.

These tests avoid actually launching Textual apps by patching the view's
`.run()` method to a no-op. The goal is to assert that the CLI handler's
branching logic (watch vs. non-watch) produces the right exit code and
constructor arguments, not to exercise the Textual render loop (which is
covered by the view-level tests).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, WorkspaceState


def _empty_workspace(tmp_path: Path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    StateManager(state_dir).save(WorkspaceState(tasks={}))
    return cfg, state_dir


@pytest.fixture
def empty_workspace(tmp_path, monkeypatch):
    cfg, state_dir = _empty_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        yield tmp_path
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
        container.state_manager.reset_override()
        container.state_manager.reset()


def test_journal_non_watch_no_task_exits_1(empty_workspace):
    runner = CliRunner()
    result = runner.invoke(app, ["view", "journal"])
    assert result.exit_code == 1
    assert "no active task" in (result.output or "").lower()


def test_journal_watch_no_task_constructs_view_without_exit(empty_workspace, monkeypatch):
    from mship.cli.view import logs as logs_mod
    captured = {}

    def _fake_run(self):
        # Capture the instance so we can assert on its state.
        captured["view"] = self

    monkeypatch.setattr(logs_mod.LogsView, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(app, ["view", "journal", "--watch"])
    assert result.exit_code == 0, result.output
    view = captured["view"]
    assert view._task_slug is None
    assert view._cli_task is None
    assert view._watch is True


def test_journal_watch_with_unknown_task_constructs_view_without_exit(empty_workspace, monkeypatch):
    from mship.cli.view import logs as logs_mod
    captured = {}
    monkeypatch.setattr(logs_mod.LogsView, "run", lambda self: captured.setdefault("view", self))
    runner = CliRunner()
    result = runner.invoke(app, ["view", "journal", "--watch", "--task", "missing"])
    assert result.exit_code == 0, result.output
    view = captured["view"]
    assert view._task_slug is None
    assert view._cli_task == "missing"
