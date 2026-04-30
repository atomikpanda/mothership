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
        sm.save(WorkspaceState(tasks={task.slug: task}))


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


def test_check_commit_no_active_tasks_exits_zero(tmp_path):
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


def _git(args: list[str], cwd: Path) -> None:
    import subprocess
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
        },
    )


def test_check_commit_dirty_toplevel_shows_recovery_commands(tmp_path):
    """When the rejected path has uncommitted changes, show per-worktree
    `git -C ... stash push` / `cd ... && git stash pop` lines so the user
    can move misrouted edits to the correct worktree."""
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()

    wt = tmp_path / "wt-cli"
    wt.mkdir()
    task = Task(
        slug="my-task", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["cli"], branch="feat/my-task",
        worktrees={"cli": wt},
    )
    _seed(tmp_path / ".mothership", task)

    # Real git repo at `wrong` with a modified-but-uncommitted file.
    wrong = tmp_path / "main-checkout"
    wrong.mkdir()
    _git(["init", "-q"], cwd=wrong)
    (wrong / "file.txt").write_text("hello\n")
    _git(["add", "file.txt"], cwd=wrong)
    _git(["commit", "-q", "-m", "init"], cwd=wrong)
    (wrong / "file.txt").write_text("dirty\n")

    try:
        result = runner.invoke(app, ["_check-commit", str(wrong)])
        assert result.exit_code == 1
        out = result.output
        # Recovery block must name the slug, reference the main-checkout path,
        # and name the worktree destination.
        assert "uncommitted changes" in out
        assert "my-task-misrouted" in out
        assert f"git -C {wrong}" in out or f"git -C {wrong.as_posix()}" in out
        assert str(wt) in out
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_clean_toplevel_no_recovery_block(tmp_path):
    """Clean rejected path → no recovery block (avoids noise when the user
    just forgot to cd, not an actual misroute)."""
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()

    wt = tmp_path / "wt-cli"
    wt.mkdir()
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["cli"], branch="feat/t",
        worktrees={"cli": wt},
    )
    _seed(tmp_path / ".mothership", task)

    wrong = tmp_path / "main-checkout"
    wrong.mkdir()
    _git(["init", "-q"], cwd=wrong)
    (wrong / "file.txt").write_text("hello\n")
    _git(["add", "file.txt"], cwd=wrong)
    _git(["commit", "-q", "-m", "init"], cwd=wrong)
    # Deliberately leave the tree clean.

    try:
        result = runner.invoke(app, ["_check-commit", str(wrong)])
        assert result.exit_code == 1
        out = result.output
        assert "uncommitted changes" not in out
        assert "stash push" not in out
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_multi_worktree_recovery_warns_pick_one(tmp_path):
    """With >1 active worktree, the recovery block still prints per-worktree
    commands but adds a 'pick one' note so the user doesn't run both."""
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()

    wt_cli = tmp_path / "wt-cli"
    wt_api = tmp_path / "wt-api"
    wt_cli.mkdir(); wt_api.mkdir()
    task = Task(
        slug="multi", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["cli", "api"], branch="feat/multi",
        worktrees={"cli": wt_cli, "api": wt_api},
    )
    _seed(tmp_path / ".mothership", task)

    wrong = tmp_path / "main-checkout"
    wrong.mkdir()
    _git(["init", "-q"], cwd=wrong)
    (wrong / "f").write_text("x")
    _git(["add", "f"], cwd=wrong)
    _git(["commit", "-q", "-m", "i"], cwd=wrong)
    (wrong / "f").write_text("y")

    try:
        result = runner.invoke(app, ["_check-commit", str(wrong)])
        assert result.exit_code == 1
        out = result.output
        assert "pick the worktree" in out
        assert str(wt_cli) in out
        assert str(wt_api) in out
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


# -----------------------------------------------------------------------------
# No-active-task + staged source files at workspace root → reject (#???).
# Closes the loophole where agents edit main directly because no worktree
# exists yet.
# -----------------------------------------------------------------------------

def _bootstrap_main_repo(workspace: Path) -> None:
    """Initialize a git repo at `workspace` so `git diff --cached` works."""
    workspace.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], cwd=workspace)
    (workspace / "README.md").write_text("hello\n")
    _git(["add", "README.md"], cwd=workspace)
    _git(["commit", "-q", "-m", "init"], cwd=workspace)


def test_check_commit_no_task_with_staged_src_rejects(tmp_path):
    """No active task + staged file under src/ at workspace root → reject."""
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    _seed(tmp_path / ".mothership")  # empty state
    _bootstrap_main_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text("x = 1\n")
    _git(["add", "src/thing.py"], cwd=tmp_path)

    try:
        result = runner.invoke(app, ["_check-commit", str(tmp_path)])
        assert result.exit_code == 1, result.output
        out = result.output
        assert "src/thing.py" in out
        assert "spawn" in out.lower()
        assert "--no-verify" in out
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_no_task_with_staged_tests_rejects(tmp_path):
    """Same rule applies to tests/** — those are typically dev-code too."""
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    _seed(tmp_path / ".mothership")
    _bootstrap_main_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    _git(["add", "tests/test_x.py"], cwd=tmp_path)

    try:
        result = runner.invoke(app, ["_check-commit", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "tests/test_x.py" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_no_task_with_doc_changes_only_allowed(tmp_path):
    """Doc/config edits at workspace root remain allowed without a task —
    the rule is narrowly about src/ and tests/."""
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    _seed(tmp_path / ".mothership")
    _bootstrap_main_repo(tmp_path)
    # Only a top-level doc change is staged.
    (tmp_path / "README.md").write_text("changed\n")
    _git(["add", "README.md"], cwd=tmp_path)

    try:
        result = runner.invoke(app, ["_check-commit", str(tmp_path)])
        assert result.exit_code == 0, result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_no_task_outside_workspace_root_allowed(tmp_path):
    """The rule only fires when toplevel == workspace root. Commits in
    unrelated repos that happen to share a no-task state must not break."""
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    _seed(tmp_path / ".mothership")

    other = tmp_path / "unrelated"
    other.mkdir()
    _git(["init", "-q"], cwd=other)
    (other / "src").mkdir()
    (other / "src" / "thing.py").write_text("x = 1\n")
    _git(["add", "src/thing.py"], cwd=other)

    try:
        # toplevel is the unrelated repo, not the workspace root.
        result = runner.invoke(app, ["_check-commit", str(other)])
        assert result.exit_code == 0, result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_no_task_no_git_repo_fails_open(tmp_path):
    """Workspace root that isn't a git repo → fail-open (existing behavior)."""
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    _seed(tmp_path / ".mothership")
    # Deliberately not a git repo.

    try:
        result = runner.invoke(app, ["_check-commit", str(tmp_path)])
        assert result.exit_code == 0, result.output
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
