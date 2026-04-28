"""End-to-end smoke test: spawn → phase → test → close."""
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner
from unittest.mock import MagicMock

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def full_workspace(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    # Provide sensible defaults for audit_repos git probes so spawn doesn't block.
    def _shell_run(cmd, cwd, env=None):
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
    mock_shell.run.side_effect = _shell_run
    container.shell.override(mock_shell)

    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_full_lifecycle(full_workspace: Path):
    # 1. Spawn
    result = runner.invoke(app, ["spawn", "add labels", "--repos", "shared,auth-service"])
    assert result.exit_code == 0, result.output
    assert "add-labels" in result.output

    # 2. Check status with explicit task
    result = runner.invoke(app, ["status", "--task", "add-labels"])
    assert result.exit_code == 0
    assert "add-labels" in result.output
    assert "plan" in result.output

    # 3. Transition to dev
    result = runner.invoke(app, ["phase", "dev", "--task", "add-labels"])
    assert result.exit_code == 0

    # 4. Check status shows dev
    result = runner.invoke(app, ["status", "--task", "add-labels"])
    assert "dev" in result.output

    # 5. Run tests
    result = runner.invoke(app, ["test", "--task", "add-labels"])
    assert result.exit_code == 0

    # 6. List worktrees
    result = runner.invoke(app, ["worktrees"])
    assert result.exit_code == 0
    assert "add-labels" in result.output

    # 7. Show graph
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared" in result.output

    # 8. Close (task was never finished, --abandon to discard without PRs)
    result = runner.invoke(app, ["close", "--yes", "--abandon", "--task", "add-labels"])
    assert result.exit_code == 0

    # 9. Status shows no task (no explicit filter; list mode reports empty)
    import json as _json
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    payload = _json.loads(result.output)
    assert payload == {"active_tasks": []}


# ---------------------------------------------------------------------------
# Audit gate integration tests
# ---------------------------------------------------------------------------

def _reset_audit_container():
    """Reset container singletons that cache state_dir-derived paths."""
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def test_spawn_blocks_when_affected_repo_is_dirty(audit_workspace):
    state_dir = audit_workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(state_dir)
    container.log_manager.reset()
    try:
        (audit_workspace / "cli" / "README.md").write_text("modified\n")
        result = runner.invoke(app, ["spawn", "dirty test", "--repos", "cli"])
        assert result.exit_code == 1
        assert "dirty_worktree" in result.output
    finally:
        _reset_audit_container()


def test_spawn_force_audit_bypasses_and_logs(audit_workspace):
    from mship.core.log import LogManager

    state_dir = audit_workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(state_dir)
    container.log_manager.reset()
    try:
        (audit_workspace / "cli" / "README.md").write_text("modified\n")
        result = runner.invoke(app, ["spawn", "force test", "--repos", "cli", "--force-audit"])
        assert result.exit_code == 0, result.output

        log_mgr = LogManager(state_dir / "logs")
        entries = log_mgr.read("force-test")
        assert any("BYPASSED AUDIT" in e.message for e in entries)
    finally:
        _reset_audit_container()


def test_full_hub_layout_e2e(tmp_path, monkeypatch):
    """Spawn → assert hub layout, sibling resolution, passive expansion, single marker."""
    import os, subprocess
    from typer.testing import CliRunner
    from mship.cli import app, container

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    # Two repos: api depends on shared
    for n in ("api", "shared"):
        d = tmp_path / n
        d.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(d)],
                       check=True, capture_output=True)
        (d / "Taskfile.yml").write_text("version: '3'\ntasks:\n  setup:\n    cmds:\n      - echo ok\n")
        (d / "README.md").write_text(n)
        subprocess.run(["git", "add", "."], cwd=d, check=True, capture_output=True, env=env)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True, capture_output=True, env=env)

    (tmp_path / "mothership.yaml").write_text(
        "workspace: e2e\nrepos:\n"
        "  api:\n    path: ./api\n    type: service\n    base_branch: main\n    expected_branch: main\n    depends_on: [shared]\n"
        "  shared:\n    path: ./shared\n    type: library\n    base_branch: main\n    expected_branch: main\n"
    )
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    container.config.reset()
    container.state_manager.reset()
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        # Spawn affected=api, expect shared to be passive
        result = runner.invoke(app, ["spawn", "feature", "--repos", "api",
                                     "--skip-setup", "--force-audit", "--offline"])
        assert result.exit_code == 0, result.output

        from mship.core.state import StateManager
        from pathlib import Path as _P
        state = StateManager(tmp_path / ".mothership").load()
        task = state.tasks["feature"]
        # Hub layout: both worktrees as siblings under <workspace>/.worktrees/feature/
        hub = tmp_path / ".worktrees" / "feature"
        assert _P(task.worktrees["api"]) == hub / "api"
        assert _P(task.worktrees["shared"]) == hub / "shared"
        # Passive set
        assert task.passive_repos == {"shared"}
        # Sibling resolution (the win case)
        from_api = _P(task.worktrees["api"]) / ".." / "shared"
        assert from_api.resolve() == _P(task.worktrees["shared"]).resolve()
        # Single .mship-workspace marker at hub root
        assert (hub / ".mship-workspace").is_file()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
