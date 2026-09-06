import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.util.git import GitRunner

runner = CliRunner()


@pytest.fixture
def configured_prune_app(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)
    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_prune_dry_run_no_orphans(configured_prune_app: Path):
    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 0
    assert "No orphaned worktrees" in result.output or "orphans" in result.output


def test_prune_dry_run_finds_orphan(configured_prune_app: Path):
    git = GitRunner()
    shared_path = configured_prune_app / "shared"
    wt_path = shared_path / ".worktrees" / "feat" / "orphan"
    git.worktree_add(repo_path=shared_path, worktree_path=wt_path, branch="feat/orphan")

    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 0
    assert wt_path.exists()  # dry run — should still exist


def test_prune_force_removes_orphan(configured_prune_app: Path):
    git = GitRunner()
    shared_path = configured_prune_app / "shared"
    wt_path = shared_path / ".worktrees" / "feat" / "orphan"
    git.worktree_add(repo_path=shared_path, worktree_path=wt_path, branch="feat/orphan")

    result = runner.invoke(app, ["prune", "--force"])
    assert result.exit_code == 0
    assert not wt_path.exists()


def test_prune_retention_conflict_is_clean_error(
    configured_prune_app: Path, monkeypatch,
):
    from datetime import datetime, timezone

    from mship.core.state import StateManager, Task, WorkspaceState
    from mship.core.workitem_lifecycle import TaskMetadataRetentionConflictError

    state_dir = configured_prune_app / ".mothership"
    state = StateManager(state_dir)
    state.save(WorkspaceState(tasks={
        "ghost": Task(
            slug="ghost", description="Ghost task", phase="dev",
            created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
            affected_repos=["shared"], branch="feat/ghost",
            worktrees={"shared": Path("/tmp/nonexistent/worktree")},
        ),
    }))

    def reject_retention(*, task, workitems_dir):
        raise TaskMetadataRetentionConflictError(task.slug, "metadata retention failed")

    monkeypatch.setattr(
        "mship.core.workitem_lifecycle.retain_workitem_metadata_on_teardown",
        reject_retention,
    )

    result = runner.invoke(app, ["prune", "--force"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Retry the operation" in result.output
    assert "Traceback" not in result.output
    assert "ghost" in state.load().tasks
