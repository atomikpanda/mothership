import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


runner = CliRunner()


def _init_repo_on_branch(path: Path, branch: str) -> None:
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True, capture_output=True)
    (path / "x.txt").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True, capture_output=True, env=env)


def _override(tmp_path: Path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def test_post_checkout_silent_on_default_branch(tmp_path, monkeypatch):
    _init_repo_on_branch(tmp_path, "main")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    _override(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["_post-checkout", "HEAD", "HEAD"])
        assert result.exit_code == 0, result.output
        assert "mship:" not in result.output
    finally:
        _reset()


def test_post_checkout_warns_when_no_active_task(tmp_path, monkeypatch):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    subprocess.run(["git", "checkout", "-qb", "feat/rogue"], cwd=tmp_path,
                   check=True, capture_output=True, env=env)
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    _override(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["_post-checkout", "HEAD", "HEAD"])
        assert result.exit_code == 0, result.output
        assert "mship spawn" in result.output
        assert "feat/rogue" in result.output
    finally:
        _reset()


def test_post_checkout_silent_when_on_task_branch_and_cwd_in_worktree(tmp_path, monkeypatch):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    wt = tmp_path / ".worktrees" / "feat-t"
    subprocess.run(["git", "worktree", "add", "-b", "feat/t", str(wt)],
                   cwd=tmp_path, check=True, capture_output=True, env=env)
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    sm = StateManager(tmp_path / ".mothership")
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/t",
        worktrees={"r": wt},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    _override(tmp_path)
    try:
        monkeypatch.chdir(wt)
        result = runner.invoke(app, ["_post-checkout", "HEAD", "HEAD"])
        assert result.exit_code == 0, result.output
        assert "mship:" not in result.output
    finally:
        _reset()


def test_post_checkout_warns_when_branch_mismatches_task(tmp_path, monkeypatch):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    # Create a different branch outside the task's expected branch
    subprocess.run(["git", "checkout", "-qb", "feat/wrong"], cwd=tmp_path,
                   check=True, capture_output=True, env=env)
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    sm = StateManager(tmp_path / ".mothership")
    task = Task(
        slug="add-labels", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/add-labels",
        worktrees={"r": tmp_path / "fake-wt"},
    )
    sm.save(WorkspaceState(current_task="add-labels", tasks={"add-labels": task}))

    _override(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["_post-checkout", "HEAD", "HEAD"])
        assert result.exit_code == 0, result.output
        assert "add-labels" in result.output
        assert "feat/add-labels" in result.output
        assert "feat/wrong" in result.output
    finally:
        _reset()


def test_post_checkout_warns_when_on_task_branch_but_not_in_worktree(tmp_path, monkeypatch):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    # Check out the task branch in the MAIN checkout (not the worktree)
    subprocess.run(["git", "checkout", "-qb", "feat/t"], cwd=tmp_path,
                   check=True, capture_output=True, env=env)
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    sm = StateManager(tmp_path / ".mothership")
    expected_wt = tmp_path / ".worktrees" / "feat-t"
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/t",
        worktrees={"r": expected_wt},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    _override(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)  # NOT the worktree
        result = runner.invoke(app, ["_post-checkout", "HEAD", "HEAD"])
        assert result.exit_code == 0, result.output
        assert str(expected_wt) in result.output
        assert "cd" in result.output.lower()
    finally:
        _reset()


def test_log_commit_appends_entry_when_in_task_worktree(tmp_path, monkeypatch):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    wt = tmp_path / ".worktrees" / "feat-t"
    subprocess.run(["git", "worktree", "add", "-b", "feat/t", str(wt)],
                   cwd=tmp_path, check=True, capture_output=True, env=env)
    # Make a commit in the worktree so `git log -1` has something to read
    (wt / "file.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "test commit subject"],
                   cwd=wt, check=True, capture_output=True, env=env)

    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    sm = StateManager(tmp_path / ".mothership")
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/t",
        worktrees={"r": wt},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    _override(tmp_path)
    try:
        monkeypatch.chdir(wt)
        result = runner.invoke(app, ["_journal-commit"])
        assert result.exit_code == 0, result.output

        from mship.core.log import LogManager
        entries = LogManager(tmp_path / ".mothership" / "logs").read("t")
        auto = [e for e in entries if e.action == "committed"]
        assert auto, [e.message for e in entries]
        assert "test commit subject" in auto[-1].message
        assert auto[-1].repo == "r"
    finally:
        _reset()


def test_log_commit_silent_when_no_active_task(tmp_path, monkeypatch):
    _init_repo_on_branch(tmp_path, "main")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    _override(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["_journal-commit"])
        assert result.exit_code == 0, result.output
    finally:
        _reset()


def test_log_commit_silent_when_cwd_not_in_worktree(tmp_path, monkeypatch):
    """--no-verify case: commit happens outside any worktree; don't log."""
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    sm = StateManager(tmp_path / ".mothership")
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/t",
        worktrees={"r": tmp_path / ".worktrees" / "feat-t"},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    _override(tmp_path)
    try:
        # cwd = main checkout, not the worktree
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["_journal-commit"])
        assert result.exit_code == 0, result.output

        from mship.core.log import LogManager
        entries = LogManager(tmp_path / ".mothership" / "logs").read("t")
        assert not any(e.action == "committed" for e in entries)
    finally:
        _reset()
