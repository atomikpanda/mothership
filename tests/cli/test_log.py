from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.log import LogManager

runner = CliRunner()


@pytest.fixture
def configured_app_with_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(current_task="add-labels", tasks={"add-labels": task}))

    # Create the log file
    log_mgr = LogManager(state_dir / "logs")
    log_mgr.create("add-labels")

    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def test_log_append(configured_app_with_task: Path):
    result = runner.invoke(app, ["journal", "Refactored auth controller"])
    assert result.exit_code == 0


def test_log_read(configured_app_with_task: Path):
    runner.invoke(app, ["journal", "First entry"])
    runner.invoke(app, ["journal", "Second entry"])
    result = runner.invoke(app, ["journal"])
    assert result.exit_code == 0
    assert "First entry" in result.output
    assert "Second entry" in result.output


def test_log_last_n(configured_app_with_task: Path):
    runner.invoke(app, ["journal", "First"])
    runner.invoke(app, ["journal", "Second"])
    runner.invoke(app, ["journal", "Third"])
    result = runner.invoke(app, ["journal", "--last", "1"])
    assert result.exit_code == 0
    assert "Third" in result.output
    assert "First" not in result.output


def test_log_no_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["journal"])
    assert result.exit_code != 0 or "No active task" in result.output
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def _setup(ws):
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(ws / ".mothership")


def _teardown():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def test_log_with_action_and_open_flags(workspace_with_git):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "flags test", "--repos", "shared", "--force-audit"])
        result = runner.invoke(
            app, ["journal", "stuck",
                    "--action", "debugging middleware",
                    "--open", "how to handle null workspace",
                    "--repo", "shared",
                    "--test-state", "fail"],
        )
        assert result.exit_code == 0, result.output

        log_mgr = LogManager(workspace_with_git / ".mothership" / "logs")
        entries = log_mgr.read("flags-test")
        assert entries
        latest = entries[-1]
        assert latest.action == "debugging middleware"
        assert latest.open_question == "how to handle null workspace"
        assert latest.repo == "shared"
        assert latest.test_state == "fail"
    finally:
        _teardown()


def test_log_infers_repo_from_active_repo(workspace_with_git, monkeypatch):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "infer test", "--repos", "shared", "--force-audit"])
        runner.invoke(app, ["switch", "shared"])
        # cd into the worktree so the cwd check passes
        from mship.core.state import StateManager
        state = StateManager(workspace_with_git / ".mothership").load()
        wt = state.tasks["infer-test"].worktrees["shared"]
        monkeypatch.chdir(wt)
        runner.invoke(app, ["journal", "did a thing"])
        log_mgr = LogManager(workspace_with_git / ".mothership" / "logs")
        entries = log_mgr.read("infer-test")
        did = next(e for e in entries if e.message == "did a thing")
        assert did.repo == "shared"
    finally:
        _teardown()


def test_log_show_open_lists_open_questions(workspace_with_git):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "open test", "--repos", "shared", "--force-audit"])
        runner.invoke(
            app, ["journal", "stuck", "--open", "how to handle nulls", "--repo", "shared"],
        )
        runner.invoke(
            app, ["journal", "also stuck", "--open", "timeout logic unclear", "--repo", "shared"],
        )
        result = runner.invoke(app, ["journal", "--show-open"])
        assert result.exit_code == 0
        assert "how to handle nulls" in result.output
        assert "timeout logic unclear" in result.output
    finally:
        _teardown()


def test_log_show_open_empty_exits_zero(workspace_with_git):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "nothing open", "--repos", "shared", "--force-audit"])
        result = runner.invoke(app, ["journal", "--show-open"])
        assert result.exit_code == 0
    finally:
        _teardown()


def test_log_refuses_when_cwd_outside_active_worktree(workspace_with_git, tmp_path, monkeypatch):
    """Default: log refuses (not just warns) when cwd is wrong."""
    from mship.cli import app, container
    from typer.testing import CliRunner
    r = CliRunner()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        r.invoke(app, ["spawn", "refuse test", "--repos", "shared", "--force-audit"])
        r.invoke(app, ["switch", "shared"])
        monkeypatch.chdir(tmp_path)
        result = r.invoke(app, ["journal", "should fail"])
        assert result.exit_code != 0
        assert "--force" in result.output or "override" in result.output.lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.log_manager.reset()


def test_log_force_writes_entry_with_bypass_tag(workspace_with_git, tmp_path, monkeypatch):
    from mship.cli import app, container
    from mship.core.log import LogManager
    from typer.testing import CliRunner
    r = CliRunner()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        r.invoke(app, ["spawn", "bypass test", "--repos", "shared", "--force-audit"])
        r.invoke(app, ["switch", "shared"])
        monkeypatch.chdir(tmp_path)
        result = r.invoke(app, ["journal", "force msg", "--force"])
        assert result.exit_code == 0, result.output
        entries = LogManager(workspace_with_git / ".mothership" / "logs").read("bypass-test")
        forced = [e for e in entries if e.message == "force msg"]
        assert forced
        assert forced[0].action is not None and "cwd-bypass" in forced[0].action
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.log_manager.reset()


def test_log_silent_when_cwd_inside_active_worktree(workspace_with_git, monkeypatch):
    from mship.cli import app, container
    from typer.testing import CliRunner
    _runner = CliRunner()

    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        _runner.invoke(app, ["spawn", "cwd test2", "--repos", "shared", "--force-audit"])
        _runner.invoke(app, ["switch", "shared"])

        # cd into the actual worktree
        from mship.core.state import StateManager
        state = StateManager(workspace_with_git / ".mothership").load()
        wt = state.tasks["cwd-test2"].worktrees["shared"]
        monkeypatch.chdir(wt)

        result = _runner.invoke(app, ["journal", "something inside"])
        assert result.exit_code == 0
        # No cwd warning when we're in the right place
        assert "⚠" not in result.output
        assert "not the active" not in result.output.lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.log_manager.reset()
