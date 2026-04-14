"""
Tests for CLI help improvements (spec: 2026-04-14-cli-help-options-design.md).

1. no_args_is_help=True on root app and view sub-app
2. _resolve_repos error lists available repos
3. mship logs <service> invalid/missing service lists available
4. mship view spec <name> SpecNotFoundError lists available specs
5. mship view logs <task-slug> unknown slug lists known tasks
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


# ---------------------------------------------------------------------------
# 1. no_args_is_help=True
# ---------------------------------------------------------------------------

def test_root_no_args_shows_help():
    result = runner.invoke(app, [])
    assert result.exit_code in (0, 2)
    assert "Usage:" in result.output


def test_view_no_args_shows_help(workspace: Path):
    """mship view with no subcommand should show help, not error."""
    container.config_path.override(workspace / "mothership.yaml")
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["view"])
        assert result.exit_code in (0, 2)
        assert "Usage:" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


# ---------------------------------------------------------------------------
# 2. _resolve_repos error lists available repos
# ---------------------------------------------------------------------------

def test_resolve_repos_unknown_lists_available(workspace: Path):
    """mship test --repos bogus should mention available repos."""
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="test-task",
        description="Test task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/test-task",
    )
    mgr.save(WorkspaceState(current_task="test-task", tasks={"test-task": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    try:
        result = runner.invoke(app, ["test", "--repos", "bogus"])
        assert result.exit_code == 1
        assert "Available:" in result.output
        # At least one real repo name should appear
        assert any(r in result.output for r in ["shared", "auth-service", "api-gateway"])
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
        container.state_manager.reset_override()
        container.state_manager.reset()
        container.shell.reset_override()


# ---------------------------------------------------------------------------
# 3. mship logs <service> invalid service lists available
# ---------------------------------------------------------------------------

def test_logs_invalid_service_lists_available(workspace: Path):
    """mship logs unknown-svc should mention available services."""
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="test-task",
        description="Test task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/test-task",
    )
    mgr.save(WorkspaceState(current_task="test-task", tasks={"test-task": task}))

    try:
        result = runner.invoke(app, ["logs", "unknown-svc"])
        assert result.exit_code == 1
        assert "Available services:" in result.output
        assert any(r in result.output for r in ["shared", "auth-service", "api-gateway"])
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
        container.state_manager.reset_override()
        container.state_manager.reset()


# ---------------------------------------------------------------------------
# 4. mship view spec <name> SpecNotFoundError lists available specs
# ---------------------------------------------------------------------------

def test_spec_not_found_lists_available(tmp_path: Path):
    """find_spec with a missing name should include available filenames in the error."""
    from mship.core.view.spec_discovery import find_spec, SpecNotFoundError

    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    # Create 7 specs to trigger the "(N more)" suffix
    for i in range(1, 8):
        (specs / f"spec-{i:02d}.md").write_text(f"# Spec {i}\n")

    with pytest.raises(SpecNotFoundError) as exc_info:
        find_spec(workspace_root=tmp_path, name_or_path="nope")

    msg = str(exc_info.value)
    # Should list at least one .md filename
    assert ".md" in msg
    # With 7 files and max 5 shown, should say "more"
    assert "more" in msg


# ---------------------------------------------------------------------------
# 5. mship view logs <task-slug> unknown slug lists known tasks
# ---------------------------------------------------------------------------

def test_view_logs_unknown_slug_lists_known(workspace: Path):
    """mship view logs bogus-slug should exit 1 and list known task slugs."""
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="real-task",
        description="Real task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/real-task",
    )
    mgr.save(WorkspaceState(current_task="real-task", tasks={"real-task": task}))

    try:
        result = runner.invoke(app, ["view", "logs", "bogus-slug"])
        assert result.exit_code == 1
        assert "real-task" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
        container.state_manager.reset_override()
        container.state_manager.reset()
