from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


@pytest.fixture
def configured_app_with_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))
    yield
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_phase_transition(configured_app_with_task, workspace: Path):
    result = runner.invoke(app, ["phase", "dev", "--task", "add-labels"])
    assert result.exit_code == 0
    mgr = StateManager(workspace / ".mothership")
    state = mgr.load()
    assert state.tasks["add-labels"].phase == "dev"


def test_phase_shows_warnings(configured_app_with_task):
    result = runner.invoke(app, ["phase", "dev", "--task", "add-labels"])
    assert "WARNING" in result.output or "spec" in result.output.lower()


def test_phase_no_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code != 0 or "no active task" in result.output.lower()
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_phase_blocked_without_force_errors(configured_app_with_task, workspace: Path):
    mgr = StateManager(workspace / ".mothership")
    state = mgr.load()
    state.tasks["add-labels"].blocked_reason = "waiting on API key"
    state.tasks["add-labels"].blocked_at = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    mgr.save(state)

    result = runner.invoke(app, ["phase", "dev", "--task", "add-labels"])
    assert result.exit_code != 0
    assert "blocked" in result.output.lower()
    assert "waiting on API key" in result.output


def test_phase_blocked_with_force_transitions(configured_app_with_task, workspace: Path):
    mgr = StateManager(workspace / ".mothership")
    state = mgr.load()
    state.tasks["add-labels"].blocked_reason = "waiting on API key"
    state.tasks["add-labels"].blocked_at = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    mgr.save(state)

    result = runner.invoke(app, ["phase", "dev", "--force", "--task", "add-labels"])
    assert result.exit_code == 0

    state = mgr.load()
    assert state.tasks["add-labels"].phase == "dev"
    assert state.tasks["add-labels"].blocked_reason is None


def test_phase_refuses_when_active_repo_is_passive(tmp_path, monkeypatch):
    """`mship phase dev` errors if active_repo is passive."""
    from datetime import datetime, timezone
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, Task, WorkspaceState

    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/x",
            worktrees={"shared": tmp_path / "wt"},
            passive_repos={"shared"},
            active_repo="shared",
        )
    }))
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)
    container.config.reset()
    container.state_manager.reset()
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["phase", "dev", "--task", "x"])
        assert result.exit_code != 0
        assert "passive" in (result.output or "").lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
