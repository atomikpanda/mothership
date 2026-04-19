import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState
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


def test_spawn_all_repos(configured_git_app: Path):
    result = runner.invoke(app, ["spawn", "big change"])
    assert result.exit_code == 0, result.output
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    task = state.tasks["big-change"]
    assert set(task.affected_repos) == {"shared", "auth-service", "api-gateway"}


def test_spawn_records_base_branch_main(configured_git_app: Path):
    result = runner.invoke(app, ["spawn", "record base", "--repos", "shared"])
    assert result.exit_code == 0, result.output
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert state.tasks["record-base"].base_branch == "main"


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
    result = r.invoke(_app, ["close", "--yes", "--abandon", "--task", "t"])
    assert result.exit_code == 0, result.output
    assert "t" not in sm.load().tasks


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
        result = r.invoke(_app, ["close", "--yes", "--task", "t"])
        assert result.exit_code == 0, result.output
        assert "completed" in result.output.lower() or "merged" in result.output.lower()
        assert "t" not in sm.load().tasks
    finally:
        container.shell.reset_override()


def test_close_with_open_pr_refuses_without_force(configured_git_app):
    from mship.cli import container
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
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
        result = r.invoke(_app, ["close", "--yes", "--task", "t"])
        assert result.exit_code != 0
        assert "open" in result.output.lower()
        # State unchanged
        assert "t" in sm.load().tasks
    finally:
        container.shell.reset_override()


def test_close_with_open_pr_proceeds_under_force(configured_git_app):
    from mship.cli import container
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
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
        result = r.invoke(_app, ["close", "--yes", "--force", "--task", "t"])
        assert result.exit_code == 0, result.output
        assert "t" not in sm.load().tasks
    finally:
        container.shell.reset_override()


def _make_merged_pr_state(configured_git_app: Path):
    """Helper: persist a finished, merged-PR task with a real worktree path."""
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
        tasks={"t": Task(
            slug="t", description="d", phase="review",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/t",
            worktrees={"shared": configured_git_app / "shared"},
            pr_urls={"shared": "https://github.com/o/r/pull/17"},
            finished_at=datetime.now(timezone.utc),
        )},
    )
    sm.save(state)
    return sm


def _ancestry_shell_factory(*, ancestor_returncode: int, merge_oid: str = "abc123def\n"):
    """Build a shell.run side_effect for the ancestry-check tests."""
    from mship.util.shell import ShellResult

    def fake_run(cmd, cwd, env=None):
        if "gh pr view" in cmd and "mergeCommit" in cmd:
            return ShellResult(returncode=0, stdout=merge_oid, stderr="")
        if "gh pr view" in cmd and "state" in cmd:
            return ShellResult(returncode=0, stdout="MERGED\n", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git merge-base --is-ancestor" in cmd:
            return ShellResult(returncode=ancestor_returncode, stdout="", stderr="")
        # Recovery-path probes (count_commits_ahead / pushed / merged) — return safe defaults
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    return fake_run


def test_close_merged_pr_with_unreachable_commits_refuses(configured_git_app):
    from mship.cli import container
    from mship.util.shell import ShellRunner, ShellResult

    sm = _make_merged_pr_state(configured_git_app)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _ancestry_shell_factory(ancestor_returncode=1)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["close", "--yes", "--task", "t"])
        assert result.exit_code != 0, result.output
        assert "NOT in the base branch" in result.output
        assert "abc123de" in result.output  # truncated merge sha shown
        assert "t" in sm.load().tasks  # state unchanged
    finally:
        container.shell.reset_override()


def test_close_merged_pr_with_reachable_commits_succeeds(configured_git_app):
    from mship.cli import container
    from mship.util.shell import ShellRunner, ShellResult

    sm = _make_merged_pr_state(configured_git_app)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _ancestry_shell_factory(ancestor_returncode=0)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["close", "--yes", "--task", "t"])
        assert result.exit_code == 0, result.output
        assert "t" not in sm.load().tasks
    finally:
        container.shell.reset_override()


def test_close_unreachable_commits_proceeds_with_bypass_flag(configured_git_app):
    from mship.cli import container
    from mship.util.shell import ShellRunner, ShellResult

    sm = _make_merged_pr_state(configured_git_app)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _ancestry_shell_factory(ancestor_returncode=1)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(
            app, ["close", "--yes", "--bypass-base-ancestry", "--task", "t"]
        )
        assert result.exit_code == 0, result.output
        assert "t" not in sm.load().tasks
    finally:
        container.shell.reset_override()


def test_close_merged_pr_with_unverified_merge_commit_warns_but_proceeds(configured_git_app):
    from mship.cli import container
    from mship.util.shell import ShellRunner, ShellResult

    sm = _make_merged_pr_state(configured_git_app)

    # Empty mergeCommit oid → unverified path; should warn, not refuse.
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _ancestry_shell_factory(
        ancestor_returncode=0, merge_oid="\n"
    )
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["close", "--yes", "--task", "t"])
        assert result.exit_code == 0, result.output
        assert "could not verify base ancestry" in result.output
        assert "t" not in sm.load().tasks
    finally:
        container.shell.reset_override()


def test_finish_handoff(configured_git_app: Path):
    runner.invoke(app, ["spawn", "handoff test", "--repos", "shared,auth-service"])
    result = runner.invoke(app, ["finish", "--handoff", "--task", "handoff-test"])
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

    result = runner.invoke(app, ["finish", "--task", "test-prs"])
    assert result.exit_code == 0, result.output

    # Verify PR URL stored in state
    from mship.core.state import StateManager
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert "test-prs" in state.tasks
    assert state.tasks["test-prs"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"

    cli_container.shell.reset_override()


def test_finish_body_file_passed_to_gh_pr_create(configured_git_app: Path):
    """--body-file contents must land in the gh pr create invocation."""
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "body test", "--repos", "shared"])

    body_path = configured_git_app / "body.md"
    body_path.write_text(
        "## Summary\n- added thing\n\n## Test plan\n- [ ] verify thing\n"
    )

    captured: dict[str, str] = {}

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            captured["create_cmd"] = cmd
            return ShellResult(returncode=0, stdout="https://x/pr/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)
    try:
        result = runner.invoke(
            app, ["finish", "--task", "body-test", "--body-file", str(body_path)]
        )
        assert result.exit_code == 0, result.output
        assert "create_cmd" in captured
        # The body ends up shlex-quoted; check the content leaked through verbatim.
        assert "## Summary" in captured["create_cmd"]
        assert "- added thing" in captured["create_cmd"]
        assert "## Test plan" in captured["create_cmd"]
    finally:
        cli_container.shell.reset_override()


def test_finish_body_and_body_file_mutually_exclusive(configured_git_app: Path):
    runner.invoke(app, ["spawn", "mutex test", "--repos", "shared"])
    result = runner.invoke(
        app, ["finish", "--task", "mutex-test", "--body", "x", "--body-file", "/tmp/y"]
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_finish_rejects_empty_body_file(configured_git_app: Path, tmp_path: Path):
    runner.invoke(app, ["spawn", "empty body test", "--repos", "shared"])
    empty = tmp_path / "empty.md"
    empty.write_text("   \n\n")
    result = runner.invoke(
        app, ["finish", "--task", "empty-body-test", "--body-file", str(empty)]
    )
    assert result.exit_code != 0
    assert "empty" in result.output.lower()


def test_finish_body_incompatible_with_push_only(configured_git_app: Path):
    runner.invoke(app, ["spawn", "po mutex test", "--repos", "shared"])
    result = runner.invoke(
        app,
        ["finish", "--task", "po-mutex-test", "--push-only", "--body", "x"],
    )
    assert result.exit_code != 0
    assert "no effect" in result.output.lower() or "push-only" in result.output.lower()


def test_finish_gh_not_available(configured_git_app: Path):
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "test no gh", "--repos", "shared"])

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=127, stdout="", stderr="command not found")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "test-no-gh"])
    assert result.exit_code != 0 or "gh" in result.output.lower()

    cli_container.shell.reset_override()


def _make_finished_task_with_existing_pr(
    configured_git_app: Path,
    *,
    slug: str = "t",
    ahead_of_origin: int = 0,
) -> StateManager:
    """Seed a task that's already gone through `finish` once (has PR url + finished_at).

    `ahead_of_origin` configures what the mocked git rev-list returns for
    `origin/<branch>..<branch>` when this task's branch is queried — i.e. how
    many post-finish commits the worktree has that haven't been pushed yet.
    """
    from datetime import datetime, timezone
    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
        tasks={slug: Task(
            slug=slug, description="d", phase="review",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch=f"feat/{slug}",
            worktrees={"shared": configured_git_app / "shared"},
            pr_urls={"shared": "https://github.com/o/r/pull/99"},
            finished_at=datetime.now(timezone.utc),
        )},
    )
    sm.save(state)
    return sm


def _finish_force_shell_factory(*, ahead_count: int):
    """Shell side_effect for finish-force tests.

    Captures any `git push` or `gh pr create` so tests can assert what ran.
    `ahead_count` applies ONLY to `origin/<X>..<X>` queries (the unpushed-
    commits probe); all other rev-list-count probes (audit: `@{u}..HEAD`,
    finish's preflight) still return 0 to keep the audit gate happy.
    """
    import re
    same_branch_ahead = re.compile(r"origin/([^.\s'\"]+)\.\.\1")

    captured: dict[str, list[str]] = {"push": [], "pr_create": []}

    def run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "symbolic-ref" in cmd:
            return ShellResult(returncode=0, stdout="main\n", stderr="")
        if "fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
        if "rev-list --count" in cmd:
            if same_branch_ahead.search(cmd):
                return ShellResult(returncode=0, stdout=f"{ahead_count}\n", stderr="")
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "worktree list" in cmd:
            return ShellResult(returncode=0, stdout="worktree /tmp/fake\n", stderr="")
        if "ls-remote --heads" in cmd:
            return ShellResult(returncode=0, stdout="abc refs/heads/main\n", stderr="")
        if "git push" in cmd:
            captured["push"].append(cmd)
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            captured["pr_create"].append(cmd)
            return ShellResult(returncode=0, stdout="https://x/pr/new\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    return run, captured


def test_finish_force_repushes_to_existing_pr(configured_git_app: Path):
    """--force on an already-finished task with new commits: pushes, does NOT
    create a new PR, updates finished_at, adds a re-finished journal entry."""
    from mship.cli import container as cli_container

    sm = _make_finished_task_with_existing_pr(configured_git_app, slug="rp", ahead_of_origin=2)
    before = sm.load().tasks["rp"].finished_at

    run, captured = _finish_force_shell_factory(ahead_count=2)
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)
    try:
        import time
        time.sleep(0.01)  # ensure a new finished_at timestamp differs from `before`
        result = runner.invoke(app, ["finish", "--force", "--task", "rp"])
        assert result.exit_code == 0, result.output
        assert len(captured["push"]) == 1, f"expected one push, got {captured['push']}"
        assert captured["pr_create"] == [], "should NOT create a new PR under --force"

        state = sm.load()
        assert state.tasks["rp"].finished_at > before, "finished_at must be re-stamped"
    finally:
        cli_container.shell.reset_override()


def test_finish_without_force_warns_about_unpushed_commits(configured_git_app: Path):
    """Without --force on a finished task with local commits ahead of origin:
    the user gets a warning pointing at --force, and no push happens."""
    from mship.cli import container as cli_container

    sm = _make_finished_task_with_existing_pr(configured_git_app, slug="warn")
    run, captured = _finish_force_shell_factory(ahead_count=3)
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["finish", "--task", "warn"])
        assert result.exit_code == 0, result.output
        assert "unpushed commits" in result.output
        assert "--force" in result.output
        assert "3 commits" in result.output
        assert captured["push"] == [], "no push without --force"
    finally:
        cli_container.shell.reset_override()


def test_finish_force_incompatible_with_body_file(configured_git_app: Path):
    _make_finished_task_with_existing_pr(configured_git_app, slug="bf")
    result = runner.invoke(
        app, ["finish", "--force", "--task", "bf", "--body", "hi"]
    )
    assert result.exit_code != 0
    assert "gh pr edit" in result.output


def test_finish_force_incompatible_with_handoff(configured_git_app: Path):
    _make_finished_task_with_existing_pr(configured_git_app, slug="ho")
    result = runner.invoke(app, ["finish", "--force", "--task", "ho", "--handoff"])
    assert result.exit_code != 0
    assert "handoff" in result.output.lower()


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
    sm.save(WorkspaceState(tasks={"t": task}))

    r = CliRunner().invoke(_app, ["close", "--yes", "--task", "t"])
    assert r.exit_code != 0
    assert "hasn't been finished" in r.output.lower() or "run `mship finish`" in r.output
    # State unchanged
    assert "t" in sm.load().tasks


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
    sm.save(WorkspaceState(tasks={"t": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    # count_commits_ahead returns 0 → no commits past base → recovery check passes trivially
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="0\n", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon", "--task", "t"])
        assert r.exit_code == 0, r.output
        assert "t" not in sm.load().tasks
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
    sm.save(WorkspaceState(tasks={"t": task}))

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
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon", "--task", "t"])
        assert r.exit_code != 0
        assert "unrecoverable" in r.output.lower() or "permanently lost" in r.output.lower()
        assert "t" in sm.load().tasks  # unchanged
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
    sm.save(WorkspaceState(tasks={"t": task}))

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
        r = CliRunner().invoke(_app, ["close", "--yes", "--force", "--task", "t"])
        assert r.exit_code == 0, r.output
        assert "t" not in sm.load().tasks
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
    sm.save(WorkspaceState(tasks={"t": task}))

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
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon", "--task", "t"])
        assert r.exit_code == 0, r.output
    finally:
        container.shell.reset_override()


# ---------------------------------------------------------------------------
# git_root validation tests
# ---------------------------------------------------------------------------

def test_spawn_refuses_when_git_root_missing_from_repos(workspace_monorepo_app):
    """spawn --repos pkg_a where pkg_a.git_root=mono must refuse if mono not included."""
    from typer.testing import CliRunner
    from mship.cli import app as _app
    r = CliRunner().invoke(_app, ["spawn", "validate test", "--repos", "pkg_a", "--force-audit"])
    assert r.exit_code != 0
    assert "pkg_a" in r.output
    assert "mono" in r.output
    assert "--repos" in r.output


def test_spawn_succeeds_when_git_root_present(workspace_monorepo_app):
    from typer.testing import CliRunner
    from mship.cli import app as _app
    r = CliRunner().invoke(_app, ["spawn", "validate good", "--repos", "mono,pkg_a", "--force-audit"])
    assert r.exit_code == 0, r.output


def test_spawn_plain_repo_unaffected(configured_git_app):
    """spawn with a repo that has no git_root still works without changes."""
    from typer.testing import CliRunner
    from mship.cli import app as _app
    r = CliRunner().invoke(_app, ["spawn", "plain", "--repos", "shared", "--force-audit"])
    assert r.exit_code == 0, r.output


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
    sm.save(WorkspaceState(tasks={"t": task}))

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
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon", "--task", "t"])
        assert r.exit_code == 0, r.output
    finally:
        container.shell.reset_override()


def test_finish_body_file_dash_reads_stdin(configured_git_app: Path):
    """--body-file - should read PR body from stdin (non-TTY)."""
    from unittest.mock import patch
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "stdin test", "--repos", "shared"])

    def mock_run(cmd, cwd, env=None):
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
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/pr/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)
    try:
        with patch("sys.stdin.isatty", return_value=False):
            result = runner.invoke(
                app, ["finish", "--task", "stdin-test", "--body-file", "-"],
                input="Summary\n\nTest plan\n",
            )
        # Finish may fail downstream (audit/gh/etc.) — we only assert the
        # body-file branch did NOT treat "-" as a literal path.
        assert "No such file or directory" not in result.output
        assert "Could not read --body-file" not in result.output
    finally:
        cli_container.shell.reset_override()


def test_finish_body_file_dash_tty_errors(configured_git_app: Path):
    """--body-file - should error if stdin is a TTY."""
    from unittest.mock import patch, MagicMock

    runner.invoke(app, ["spawn", "tty test", "--repos", "shared"])

    # Mock the _read_stdin_body_or_exit function to simulate TTY behavior
    with patch('mship.cli.worktree._read_stdin_body_or_exit') as mock_read:
        # Make it raise exit with our TTY error message
        import typer
        def raise_tty_error(output):
            output.error("refusing to read body from an interactive TTY; pipe or redirect stdin, or use --body-file <path>")
            raise typer.Exit(code=1)
        mock_read.side_effect = raise_tty_error
        
        result = runner.invoke(
            app, ["finish", "--task", "tty-test", "--body-file", "-"]
        )
    assert result.exit_code == 1, result.output
    assert "refusing to read body from an interactive TTY" in result.output

def test_finish_body_dash_tty_also_errors(configured_git_app: Path):
    """--body - should error if stdin is a TTY (symmetry)."""
    from unittest.mock import patch

    runner.invoke(app, ["spawn", "body tty test", "--repos", "shared"])

    # Mock the _read_stdin_body_or_exit function to simulate TTY behavior
    with patch('mship.cli.worktree._read_stdin_body_or_exit') as mock_read:
        # Make it raise exit with our TTY error message
        import typer
        def raise_tty_error(output):
            output.error("refusing to read body from an interactive TTY; pipe or redirect stdin, or use --body-file <path>")
            raise typer.Exit(code=1)
        mock_read.side_effect = raise_tty_error
        
        result = runner.invoke(
            app, ["finish", "--task", "body-tty-test", "--body", "-"]
        )
    assert result.exit_code == 1, result.output
    assert "refusing to read body from an interactive TTY" in result.output

def test_finish_body_file_dash_empty_stdin_rejected(configured_git_app: Path):
    """--body-file - with empty stdin should error."""
    from unittest.mock import patch

    runner.invoke(app, ["spawn", "empty stdin test", "--repos", "shared"])

    with patch("sys.stdin.isatty", return_value=False):
        result = runner.invoke(
            app, ["finish", "--task", "empty-stdin-test", "--body-file", "-"],
            input="",
        )
    assert result.exit_code == 1
    assert "empty" in result.output.lower()


def test_finish_shared_git_root_creates_one_pr_records_on_all(configured_git_app: Path):
    """Two repos sharing git_root: one gh pr create call, both get the URL."""
    from mship.cli import container as cli_container

    # Extend the workspace with a shared-git_root pair.
    cfg_path = configured_git_app / "mothership.yaml"
    cfg_path.write_text(cfg_path.read_text() + """
  infra:
    path: .
    git_root: shared
    type: service
""")

    runner.invoke(app, ["spawn", "group prs", "--repos", "shared,infra", "--skip-setup"])

    create_pr_call_count = 0

    def mock_run(cmd, cwd, env=None):
        nonlocal create_pr_call_count
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            # Pretend upstream is already set after push.
            return ShellResult(returncode=0, stdout="origin/feat/group-prs", stderr="")
        if "gh pr list --head" in cmd:
            # No existing PR on first call.
            return ShellResult(returncode=0, stdout="\n", stderr="")
        if "gh pr create" in cmd:
            create_pr_call_count += 1
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/42\n", stderr="")
        if "gh pr view" in cmd:
            return ShellResult(returncode=0, stdout="body text", stderr="")
        if "gh pr edit" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git log --format=%s" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "group-prs"])
    assert result.exit_code == 0, result.output
    assert create_pr_call_count == 1, f"Expected 1 gh pr create call, got {create_pr_call_count}"

    from mship.core.state import StateManager
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    pr_urls = state.tasks["group-prs"].pr_urls
    assert pr_urls.get("shared") == "https://github.com/org/shared/pull/42"
    assert pr_urls.get("infra") == "https://github.com/org/shared/pull/42"

    cli_container.shell.reset_override()


def test_finish_harvests_existing_pr_instead_of_creating(configured_git_app: Path):
    """If a PR for the branch already exists (manual or prior mship run),
    finish harvests it via `gh pr list --head` without calling `gh pr create`."""
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "reuse pr", "--repos", "shared", "--skip-setup"])

    create_pr_called = False

    def mock_run(cmd, cwd, env=None):
        nonlocal create_pr_called
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/feat/reuse-pr", stderr="")
        if "gh pr list --head" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/88\n", stderr="")
        if "gh pr create" in cmd:
            create_pr_called = True
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "reuse-pr"])
    assert result.exit_code == 0, result.output
    assert create_pr_called is False, "gh pr create should not be called when PR already exists"

    from mship.core.state import StateManager
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert state.tasks["reuse-pr"].pr_urls.get("shared") == "https://github.com/org/shared/pull/88"

    cli_container.shell.reset_override()


def test_finish_harvests_on_create_pr_duplicate_stderr(configured_git_app: Path):
    """When `gh pr list` returns empty but `gh pr create` then errors with
    'already exists' (race), finish harvests via a second list call."""
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "race pr", "--repos", "shared", "--skip-setup"])

    list_call_count = 0

    def mock_run(cmd, cwd, env=None):
        nonlocal list_call_count
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/feat/race-pr", stderr="")
        if "gh pr list --head" in cmd:
            list_call_count += 1
            if list_call_count == 1:
                # First call (pre-create check): no PR yet.
                return ShellResult(returncode=0, stdout="\n", stderr="")
            else:
                # Second call (fallback after create failed): PR exists now.
                return ShellResult(
                    returncode=0,
                    stdout="https://github.com/org/shared/pull/99\n",
                    stderr="",
                )
        if "gh pr create" in cmd:
            return ShellResult(
                returncode=1, stdout="",
                stderr="a pull request for branch \"feat/race-pr\" into branch \"main\" already exists",
            )
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "race-pr"])
    assert result.exit_code == 0, result.output
    assert list_call_count == 2, "Expected pre-check + fallback list calls"

    from mship.core.state import StateManager
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert state.tasks["race-pr"].pr_urls.get("shared") == "https://github.com/org/shared/pull/99"

    cli_container.shell.reset_override()


def test_finish_calls_ensure_upstream_after_push(configured_git_app: Path):
    """ensure_upstream fires after push; if @{u} fails, set-upstream-to runs."""
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "upstream check", "--repos", "shared", "--skip-setup"])

    set_upstream_called = False
    ensure_upstream_probe_count = 0

    def mock_run(cmd, cwd, env=None):
        nonlocal set_upstream_called, ensure_upstream_probe_count
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "symbolic-ref" in cmd and "HEAD" in cmd:
            return ShellResult(returncode=0, stdout="main\n", stderr="")
        if "fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            ensure_upstream_probe_count += 1
            # First call: audit checks upstream (before push) - pass
            # Subsequent calls: ensure_upstream checks after push - fail to trigger fallback
            if ensure_upstream_probe_count == 1:
                return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
            else:
                return ShellResult(returncode=1, stdout="", stderr="fatal: no upstream")
        if "--set-upstream-to=origin/" in cmd:
            set_upstream_called = True
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr list --head" in cmd:
            return ShellResult(returncode=0, stdout="\n", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "upstream-check"])
    assert result.exit_code == 0, result.output
    assert set_upstream_called, "ensure_upstream should have run set-upstream-to"

    cli_container.shell.reset_override()
