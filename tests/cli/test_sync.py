from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.workspace_meta import read_last_sync_at

runner = CliRunner()


def test_sync_clean_exits_zero(audit_workspace):
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")
    try:
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output or "up_to_date" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_sync_dirty_nonzero(audit_workspace):
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")
    try:
        (audit_workspace / "cli" / "README.md").write_text("modified\n")
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "dirty_worktree" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_sync_records_last_workspace_fetch_at(audit_workspace):
    state_dir = audit_workspace / ".mothership"
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(state_dir)
    try:
        assert read_last_sync_at(state_dir) is None
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert read_last_sync_at(state_dir) is not None
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_sync_refreshes_passive_worktree(tmp_path, monkeypatch):
    """`mship sync` re-fetches and resets passive worktrees to origin/<ref>."""
    import os
    import subprocess
    from datetime import datetime, timezone
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, Task, WorkspaceState

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    bare = tmp_path / "shared.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    src = tmp_path / "shared"
    subprocess.run(["git", "clone", "-q", str(bare), str(src)],
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "c1"],
                   cwd=src, check=True, capture_output=True, env=env)
    sha1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src,
                          check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)
    (src / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "c2"], cwd=src,
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)
    sha2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src,
                          check=True, capture_output=True, text=True).stdout.strip()

    # Passive worktree at OLD sha1
    passive = tmp_path / ".worktrees" / "x" / "shared"
    subprocess.run(["git", "worktree", "add", "--detach", str(passive), sha1],
                   cwd=src, check=True, capture_output=True)

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
        "    base_branch: main\n    expected_branch: main\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/x",
            worktrees={"shared": passive},
            passive_repos={"shared"},
        )
    }))

    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    container.config.reset()
    container.state_manager.reset()
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["sync"])
        # Exit code can be 0 or 1 depending on canonical audit. We care about
        # the passive worktree's HEAD afterwards.
        head_after = subprocess.run(
            ["git", "-C", str(passive), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert head_after == sha2, (
            f"passive worktree should reset to origin/main HEAD ({sha2}), "
            f"got {head_after}; CLI output: {result.output}"
        )
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_sync_no_passive_skips_passive_refresh(tmp_path, monkeypatch):
    """`mship sync --no-passive` leaves passive worktrees alone."""
    import os
    import subprocess
    from datetime import datetime, timezone
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, Task, WorkspaceState

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    bare = tmp_path / "shared.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    src = tmp_path / "shared"
    subprocess.run(["git", "clone", "-q", str(bare), str(src)],
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "c1"],
                   cwd=src, check=True, capture_output=True, env=env)
    sha1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src,
                          check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)
    (src / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "c2"], cwd=src,
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)

    passive = tmp_path / ".worktrees" / "x" / "shared"
    subprocess.run(["git", "worktree", "add", "--detach", str(passive), sha1],
                   cwd=src, check=True, capture_output=True)

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
        "    base_branch: main\n    expected_branch: main\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/x",
            worktrees={"shared": passive},
            passive_repos={"shared"},
        )
    }))

    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    container.config.reset()
    container.state_manager.reset()
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        runner.invoke(app, ["sync", "--no-passive"])
        head_after = subprocess.run(
            ["git", "-C", str(passive), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert head_after == sha1, "with --no-passive, passive worktree HEAD must NOT change"
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
