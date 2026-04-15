import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.hooks import HOOK_MARKER_BEGIN, is_installed


runner = CliRunner()


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True, env=env)


@pytest.fixture
def workspace_for_hooks(tmp_path: Path):
    """Fresh single-repo workspace with a real git init."""
    repo = tmp_path / "cli"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks:\n  setup:\n    cmds:\n      - echo setup\n")
    (repo / "README.md").write_text("cli\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")

    (tmp_path / "mothership.yaml").write_text(
        "workspace: hooktest\n"
        "repos:\n"
        "  cli:\n"
        "    path: ./cli\n"
        "    type: service\n"
    )
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    yield tmp_path, repo
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_install_hooks_flag_installs_on_every_git_root(workspace_for_hooks):
    tmp_path, repo = workspace_for_hooks
    result = runner.invoke(app, ["init", "--install-hooks"])
    assert result.exit_code == 0, result.output
    assert is_installed(repo)


def test_install_hooks_is_idempotent(workspace_for_hooks):
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])
    before = (repo / ".git" / "hooks" / "pre-commit").read_text()
    runner.invoke(app, ["init", "--install-hooks"])
    after = (repo / ".git" / "hooks" / "pre-commit").read_text()
    assert before == after


def test_commit_outside_task_worktree_refused(workspace_for_hooks):
    """End-to-end: spawn creates a worktree; a commit in the main checkout is refused."""
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])
    r = runner.invoke(app, ["spawn", "add avatars", "--repos", "cli", "--force-audit", "--skip-setup"])
    assert r.exit_code == 0, r.output

    # Make a change in the main checkout (wrong place)
    (repo / "new.py").write_text("print('hi')\n")
    _git(repo, "add", "new.py")

    # Attempt commit — hook should refuse
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    result = subprocess.run(
        ["git", "commit", "-m", "should refuse"],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "add-avatars" in result.stderr or "refusing commit" in result.stderr.lower()


def test_commit_inside_worktree_succeeds(workspace_for_hooks):
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])
    r = runner.invoke(app, ["spawn", "inside ok", "--repos", "cli", "--force-audit", "--skip-setup"])
    assert r.exit_code == 0, r.output

    from mship.core.state import StateManager
    state = StateManager(tmp_path / ".mothership").load()
    wt = Path(state.tasks["inside-ok"].worktrees["cli"])
    assert wt.exists()

    # Commit in the worktree — hook should pass
    (wt / "new.py").write_text("print('hi')\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "new.py"], cwd=wt, check=True, capture_output=True, env=env)
    result = subprocess.run(
        ["git", "commit", "-m", "from worktree"],
        cwd=wt, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_no_verify_bypasses_hook(workspace_for_hooks):
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])
    runner.invoke(app, ["spawn", "bypass test", "--repos", "cli", "--force-audit", "--skip-setup"])

    (repo / "new.py").write_text("x\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _git(repo, "add", "new.py")
    result = subprocess.run(
        ["git", "commit", "--no-verify", "-m", "bypassed"],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_init_installs_all_three_hooks(workspace_for_hooks):
    tmp_path, repo = workspace_for_hooks
    result = runner.invoke(app, ["init", "--install-hooks"])
    assert result.exit_code == 0, result.output
    hooks = repo / ".git" / "hooks"
    assert (hooks / "pre-commit").exists()
    assert (hooks / "post-checkout").exists()
    assert (hooks / "post-commit").exists()


def test_post_checkout_warns_on_rogue_branch(workspace_for_hooks):
    """git checkout -b outside mship spawn fires a stderr warning."""
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    result = subprocess.run(
        ["git", "checkout", "-b", "feat/rogue"],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    # Checkout itself succeeds — post-checkout only warns
    assert result.returncode == 0
    assert "mship spawn" in result.stderr


def test_post_commit_auto_logs_in_worktree(workspace_for_hooks):
    """A commit inside a task worktree triggers an auto-log entry."""
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])
    spawn_result = runner.invoke(
        app, ["spawn", "auto log test", "--repos", "cli", "--force-audit", "--skip-setup"],
    )
    assert spawn_result.exit_code == 0, spawn_result.output

    from mship.core.state import StateManager
    state = StateManager(tmp_path / ".mothership").load()
    wt = Path(state.tasks["auto-log-test"].worktrees["cli"])
    assert wt.exists()

    (wt / "file.txt").write_text("hello\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True, env=env)
    result = subprocess.run(
        ["git", "commit", "-m", "auto logged"],
        cwd=wt, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    from mship.core.log import LogManager
    entries = LogManager(tmp_path / ".mothership" / "logs").read("auto-log-test")
    auto = [e for e in entries if e.action == "committed"]
    assert auto
    assert "auto logged" in auto[-1].message
    assert auto[-1].repo == "cli"
