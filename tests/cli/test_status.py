import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


def _seed(path, task: Task):
    sm = StateManager(path / ".mothership")
    sm.save(WorkspaceState(tasks={task.slug: task}))


@pytest.fixture
def configured_app(workspace: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    (workspace / ".mothership").mkdir(exist_ok=True)
    yield
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_status_no_task(configured_app):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # Bimodal: non-TTY emits JSON workspace summary when no task resolves.
    payload = json.loads(result.output)
    # No active tasks → cwd check is not applicable.
    assert payload.get("active_tasks") == []


def test_status_cwd_outside_worktrees_reports_true(workspace_with_git, monkeypatch, tmp_path):
    """With an active task whose worktree is elsewhere, cwd outside → True. See #80."""
    wt_path = tmp_path / "fake-wt"
    wt_path.mkdir()
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["shared"], branch="feat/t",
        worktrees={"shared": str(wt_path)},
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        # Chdir to workspace root — outside the worktree.
        monkeypatch.chdir(workspace_with_git)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # Flag surfaces regardless of which path renders (single-task auto-resolve
        # or active_tasks summary).
        assert payload.get("cwd_is_outside_worktrees") is True
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_status_cwd_inside_worktree_reports_false(workspace_with_git, monkeypatch, tmp_path):
    """Inside any active task's worktree → False."""
    wt_path = tmp_path / "fake-wt"
    wt_path.mkdir()
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["shared"], branch="feat/t",
        worktrees={"shared": str(wt_path)},
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        monkeypatch.chdir(wt_path)
        # No task flag — auto-resolution will land on task `t` (single active),
        # so this exercises the single-task path where the field is still False.
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # Either way, the field should be False when cwd is inside the worktree.
        assert payload.get("cwd_is_outside_worktrees") is False
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_status_no_active_tasks_omits_cwd_field(configured_app):
    """When there are zero tasks with worktrees, the cwd warning is not applicable."""
    result = runner.invoke(app, ["status"])
    payload = json.loads(result.output)
    # The field must not be True; either absent or False is acceptable.
    assert payload.get("cwd_is_outside_worktrees", False) is False


def test_status_with_task(configured_app, workspace: Path):
    state_dir = workspace / ".mothership"
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels to tasks",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "add-labels" in result.output
    assert "dev" in result.output


def test_graph(configured_app):
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared" in result.output
    assert "auth-service" in result.output
    assert "api-gateway" in result.output


def test_status_shows_phase_duration_and_drift(workspace_with_git):
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
        phase_entered_at=datetime.now(timezone.utc) - timedelta(hours=3),
        affected_repos=["shared"], branch="feat/t",
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        result = runner.invoke(app, ["status", "--task", "t"])
        assert result.exit_code == 0, result.output
        # CliRunner is non-TTY so output is JSON; assert the enriched fields are present.
        payload = json.loads(result.output)
        assert payload["phase_entered_at"] is not None  # phase duration encoded
        assert "drift" in payload  # drift field present
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_status_json_includes_new_fields(workspace_with_git):
    task = Task(
        slug="t", description="d", phase="review",
        created_at=datetime.now(timezone.utc),
        phase_entered_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        affected_repos=["shared"], branch="feat/t",
        finished_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        # Non-TTY → JSON
        result = runner.invoke(app, ["status", "--task", "t"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["finished_at"] is not None
        assert payload["phase_entered_at"] is not None
        assert "drift" in payload
        assert "last_log" in payload
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_status_shows_finished_warning(workspace_with_git):
    task = Task(
        slug="t", description="d", phase="review",
        created_at=datetime.now(timezone.utc),
        phase_entered_at=datetime.now(timezone.utc),
        affected_repos=["shared"], branch="feat/t",
        finished_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        result = runner.invoke(app, ["status", "--task", "t"])
        assert result.exit_code == 0, result.output
        assert "Finished" in result.output or "finished" in result.output
        assert "mship close" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_status_shows_active_repo(workspace_with_git):
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        phase_entered_at=datetime.now(timezone.utc),
        affected_repos=["shared", "auth-service"], branch="feat/t",
        active_repo="shared",
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        result = runner.invoke(app, ["status", "--task", "t"])
        assert result.exit_code == 0, result.output
        import json as _j
        try:
            payload = _j.loads(result.output)
            assert payload["active_repo"] == "shared"
        except _j.JSONDecodeError:
            assert "Active repo" in result.output
            assert "shared" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


# --- Task 5: bimodal status tests ---------------------------------------


def _mk_workspace(tmp_path, tasks: dict[str, str]):
    """Create a workspace with the given {slug: phase} map."""
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state = WorkspaceState(tasks={
        slug: Task(
            slug=slug, description="d", phase=phase,
            created_at=datetime(2026, 4, 16, tzinfo=timezone.utc),
            affected_repos=[], branch=f"feat/{slug}",
        )
        for slug, phase in tasks.items()
    })
    StateManager(state_dir).save(state)

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    return state_dir, cfg


def _reset_container():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_status_no_tasks_emits_empty_active_list(tmp_path, monkeypatch):
    _mk_workspace(tmp_path, {})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    try:
        import json
        runner = CliRunner()
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert data == {"active_tasks": []}
    finally:
        _reset_container()


def test_status_multiple_tasks_no_anchor_lists_all(tmp_path, monkeypatch):
    _mk_workspace(tmp_path, {"A": "dev", "B": "review"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    try:
        import json
        runner = CliRunner()
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        slugs = [t["slug"] for t in data["active_tasks"]]
        assert set(slugs) == {"A", "B"}
        for t in data["active_tasks"]:
            assert "slug" in t and "phase" in t and "branch" in t
    finally:
        _reset_container()


def test_status_resolves_via_task_flag(tmp_path, monkeypatch):
    _mk_workspace(tmp_path, {"A": "dev", "B": "review"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    try:
        import json
        runner = CliRunner()
        result = runner.invoke(app, ["status", "--task", "A"])
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["slug"] == "A"
    finally:
        _reset_container()
