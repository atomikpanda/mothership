from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


runner = CliRunner()


def _seed(state_dir: Path, task: Task | None = None):
    sm = StateManager(state_dir)
    if task is None:
        sm.save(WorkspaceState())
    else:
        sm.save(WorkspaceState(current_task=task.slug, tasks={task.slug: task}))


def test_check_commit_no_state_file_exits_zero(tmp_path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    try:
        result = runner.invoke(app, ["_check-commit", str(tmp_path)])
        assert result.exit_code == 0, result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_no_active_task_exits_zero(tmp_path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    _seed(tmp_path / ".mothership")  # empty state
    try:
        result = runner.invoke(app, ["_check-commit", str(tmp_path)])
        assert result.exit_code == 0, result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_matching_worktree_exits_zero(tmp_path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["cli"], branch="feat/t",
        worktrees={"cli": wt},
    )
    _seed(tmp_path / ".mothership", task)
    try:
        result = runner.invoke(app, ["_check-commit", str(wt)])
        assert result.exit_code == 0, result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_wrong_toplevel_exits_one_with_paths(tmp_path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    wt_cli = tmp_path / "wt-cli"
    wt_api = tmp_path / "wt-api"
    wt_cli.mkdir()
    wt_api.mkdir()
    task = Task(
        slug="add-labels", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["cli", "api"], branch="feat/add-labels",
        worktrees={"cli": wt_cli, "api": wt_api},
    )
    _seed(tmp_path / ".mothership", task)
    try:
        wrong = tmp_path / "elsewhere"
        wrong.mkdir()
        result = runner.invoke(app, ["_check-commit", str(wrong)])
        assert result.exit_code == 1
        out = result.output
        assert "add-labels" in out
        assert str(wt_cli) in out
        assert str(wt_api) in out
        assert str(wrong) in out
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_fails_open_on_corrupt_state(tmp_path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    (tmp_path / ".mothership" / "state.yaml").write_text("not: valid: yaml: [[[")
    try:
        result = runner.invoke(app, ["_check-commit", str(tmp_path)])
        assert result.exit_code == 0, result.output  # fail-open
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
