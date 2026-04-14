import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


def _audit_ok_run(cmd, cwd, env=None):
    """Default shell.run side_effect that satisfies audit_repos probes cleanly."""
    if "symbolic-ref" in cmd:
        return ShellResult(returncode=0, stdout="main\n", stderr="")
    if "fetch" in cmd:
        return ShellResult(returncode=0, stdout="", stderr="")
    if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
        return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
    if "rev-list --count" in cmd:
        return ShellResult(returncode=0, stdout="0\n", stderr="")
    if "status --porcelain" in cmd:
        return ShellResult(returncode=0, stdout="", stderr="")
    if "worktree list" in cmd:
        return ShellResult(returncode=0, stdout="worktree /tmp/fake\n", stderr="")
    return ShellResult(returncode=0, stdout="", stderr="")


@pytest.fixture
def configured_git_app(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _audit_ok_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()
    container.shell.reset_override()


def test_spawn(configured_git_app: Path):
    result = runner.invoke(app, ["spawn", "add labels to tasks", "--repos", "shared"])
    assert result.exit_code == 0, result.output
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert "add-labels-to-tasks" in state.tasks
    assert state.current_task == "add-labels-to-tasks"


def test_spawn_all_repos(configured_git_app: Path):
    result = runner.invoke(app, ["spawn", "big change"])
    assert result.exit_code == 0, result.output
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    task = state.tasks["big-change"]
    assert set(task.affected_repos) == {"shared", "auth-service", "api-gateway"}


def test_worktrees_list(configured_git_app: Path):
    runner.invoke(app, ["spawn", "test list", "--repos", "shared"])
    result = runner.invoke(app, ["worktrees"])
    assert result.exit_code == 0
    assert "test-list" in result.output


def test_close_with_no_prs_cancelled_before_finish(configured_git_app):
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="dev",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/t",
        )},
    )
    sm.save(state)

    from typer.testing import CliRunner
    from mship.cli import app as _app
    r = CliRunner()
    # Task was never finished → must use --abandon to close without PRs
    result = r.invoke(_app, ["close", "--yes", "--abandon"])
    assert result.exit_code == 0, result.output
    assert sm.load().current_task is None


def test_close_no_active_task_errors():
    from typer.testing import CliRunner
    from mship.cli import app as _app
    r = CliRunner()
    result = r.invoke(_app, ["close", "--yes"])
    # Either No active task, or config-not-found — both exit 1.
    assert result.exit_code != 0


def test_close_with_all_merged_prs(configured_git_app):
    from mship.cli import container
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="review",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/t",
            pr_urls={"shared": "https://github.com/o/r/pull/1"},
            finished_at=datetime.now(timezone.utc),
        )},
    )
    sm.save(state)

    from unittest.mock import MagicMock
    from mship.util.shell import ShellRunner, ShellResult
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="MERGED\n", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        from typer.testing import CliRunner
        from mship.cli import app as _app
        r = CliRunner()
        result = r.invoke(_app, ["close", "--yes"])
        assert result.exit_code == 0, result.output
        assert "completed" in result.output.lower() or "merged" in result.output.lower()
        assert sm.load().current_task is None
    finally:
        container.shell.reset_override()


def test_close_with_open_pr_refuses_without_force(configured_git_app):
    from mship.cli import container
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="review",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/t",
            pr_urls={"shared": "https://x/1"},
            finished_at=datetime.now(timezone.utc),
        )},
    )
    sm.save(state)

    from unittest.mock import MagicMock
    from mship.util.shell import ShellRunner, ShellResult
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="OPEN\n", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        from typer.testing import CliRunner
        from mship.cli import app as _app
        r = CliRunner()
        result = r.invoke(_app, ["close", "--yes"])
        assert result.exit_code != 0
        assert "open" in result.output.lower()
        # State unchanged
        assert sm.load().current_task == "t"
    finally:
        container.shell.reset_override()


def test_close_with_open_pr_proceeds_under_force(configured_git_app):
    from mship.cli import container
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="review",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/t",
            pr_urls={"shared": "https://x/1"},
            finished_at=datetime.now(timezone.utc),
        )},
    )
    sm.save(state)

    from unittest.mock import MagicMock
    from mship.util.shell import ShellRunner, ShellResult
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="OPEN\n", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        from typer.testing import CliRunner
        from mship.cli import app as _app
        r = CliRunner()
        result = r.invoke(_app, ["close", "--yes", "--force"])
        assert result.exit_code == 0, result.output
        assert sm.load().current_task is None
    finally:
        container.shell.reset_override()


def test_finish_handoff(configured_git_app: Path):
    runner.invoke(app, ["spawn", "handoff test", "--repos", "shared,auth-service"])
    result = runner.invoke(app, ["finish", "--handoff"])
    assert result.exit_code == 0
    handoff_file = configured_git_app / ".mothership" / "handoffs" / "handoff-test.yaml"
    assert handoff_file.exists()


def test_finish_creates_prs(configured_git_app: Path):
    from mship.cli import container as cli_container

    # Spawn a task first
    runner.invoke(app, ["spawn", "test prs", "--repos", "shared"])

    # Mock shell for finish operations
    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        if "gh pr view" in cmd:
            return ShellResult(returncode=0, stdout="body text", stderr="")
        if "gh pr edit" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    # Verify PR URL stored in state
    from mship.core.state import StateManager
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert "test-prs" in state.tasks
    assert state.tasks["test-prs"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"

    cli_container.shell.reset_override()


def test_finish_gh_not_available(configured_git_app: Path):
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "test no gh", "--repos", "shared"])

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=127, stdout="", stderr="command not found")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish"])
    assert result.exit_code != 0 or "gh" in result.output.lower()

    cli_container.shell.reset_override()


def test_spawn_skip_setup_flag(configured_git_app: Path):
    """--skip-setup should skip the setup task."""
    from mship.cli import container as cli_container

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _audit_ok_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["spawn", "skip flag test", "--repos", "shared", "--skip-setup"])
    assert result.exit_code == 0, result.output
    # run_task should not have been called for setup
    mock_shell.run_task.assert_not_called()

    cli_container.shell.reset_override()


def test_spawn_shows_setup_warnings(configured_git_app: Path):
    """Setup failures should appear as warnings in output."""
    from mship.cli import container as cli_container

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _audit_ok_run
    mock_shell.run_task.return_value = ShellResult(
        returncode=1, stdout="", stderr="pnpm install failed"
    )
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["spawn", "warning flag test", "--repos", "shared"])
    assert result.exit_code == 0, result.output
    # Setup failure should appear in output as a warning
    assert "setup failed" in result.output.lower() or "pnpm install failed" in result.output

    cli_container.shell.reset_override()


# ---------------------------------------------------------------------------
# Task 2: close finish-required + recovery-path gate tests
# ---------------------------------------------------------------------------

def _build_close_task(slug="t", finished=False, pr_urls=None, worktrees=None, branch="feat/t"):
    from datetime import datetime, timezone
    from mship.core.state import Task
    return Task(
        slug=slug, description="d", phase="review",
        created_at=datetime.now(timezone.utc),
        affected_repos=list((worktrees or {}).keys()),
        branch=branch,
        worktrees=worktrees or {},
        pr_urls=pr_urls or {},
        finished_at=datetime.now(timezone.utc) if finished else None,
    )


def test_close_refuses_when_not_finished_without_abandon(configured_git_app):
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    task = _build_close_task(finished=False)
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    r = CliRunner().invoke(_app, ["close", "--yes"])
    assert r.exit_code != 0
    assert "hasn't been finished" in r.output.lower() or "run `mship finish`" in r.output
    # State unchanged
    assert sm.load().current_task == "t"


def test_close_abandon_proceeds_when_no_commits(configured_git_app):
    """No commits past base → recoverable trivially; --abandon closes cleanly."""
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from mship.util.shell import ShellRunner, ShellResult
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    task = _build_close_task(finished=False)
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    # count_commits_ahead returns 0 → no commits past base → recovery check passes trivially
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="0\n", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon"])
        assert r.exit_code == 0, r.output
        assert sm.load().current_task is None
    finally:
        container.shell.reset_override()


def test_close_abandon_refuses_when_unrecoverable(configured_git_app):
    """Commits past base, not merged, not pushed, no PR → refuses without --force."""
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from mship.util.shell import ShellRunner, ShellResult
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    # worktrees dict must map repo → path; use a dummy path that need not exist,
    # since the recovery check guards on path existence before calling git.
    task = _build_close_task(
        finished=False,
        worktrees={"shared": configured_git_app / "shared"},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    def mock_run(cmd, cwd, env=None):
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="3\n", stderr="")  # 3 commits past base
        if "git merge-base --is-ancestor" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")  # not merged
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")  # not pushed
        if "git rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="abc123\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon"])
        assert r.exit_code != 0
        assert "unrecoverable" in r.output.lower() or "permanently lost" in r.output.lower()
        assert sm.load().current_task == "t"  # unchanged
    finally:
        container.shell.reset_override()


def test_close_force_bypasses_recovery_check(configured_git_app):
    """--force destroys unrecoverable work on purpose."""
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from mship.util.shell import ShellRunner, ShellResult
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    task = _build_close_task(
        finished=False,
        worktrees={"shared": configured_git_app / "shared"},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    def mock_run(cmd, cwd, env=None):
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="3\n", stderr="")
        if "git merge-base --is-ancestor" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="abc123\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--force"])
        assert r.exit_code == 0, r.output
        assert sm.load().current_task is None
    finally:
        container.shell.reset_override()


def test_close_abandon_proceeds_when_merged(configured_git_app):
    """Commits past base but merged into base → recoverable."""
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from mship.util.shell import ShellRunner, ShellResult
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    task = _build_close_task(
        finished=False,
        worktrees={"shared": configured_git_app / "shared"},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    def mock_run(cmd, cwd, env=None):
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="3\n", stderr="")
        if "git merge-base --is-ancestor" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")  # is ancestor → merged
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon"])
        assert r.exit_code == 0, r.output
    finally:
        container.shell.reset_override()


def test_close_abandon_proceeds_when_pushed(configured_git_app):
    """Commits past base and pushed to origin → recoverable."""
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from mship.util.shell import ShellRunner, ShellResult
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    task = _build_close_task(
        finished=False,
        worktrees={"shared": configured_git_app / "shared"},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    def mock_run(cmd, cwd, env=None):
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="3\n", stderr="")
        if "git merge-base --is-ancestor" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")  # not merged
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/feat/t\n", stderr="")
        if "git rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="abc123\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon"])
        assert r.exit_code == 0, r.output
    finally:
        container.shell.reset_override()
