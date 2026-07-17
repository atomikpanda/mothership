"""issue 366 #5: a config-only main-checkout edit (mothership.yaml / Taskfile)
does not block `mship finish` on dirty_worktree; any non-config drift re-blocks.

The porcelain is CLEAN during spawn (so spawn's own audit passes regardless of
config.audit.block_spawn) and is flipped to the dirty state only for the finish
call, isolating the finish gate behaviour under test.
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.core.workitem_store import WorkItemStore
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def cfg_only_workspace(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    porcelain = {"out": ""}  # mutable; starts clean so spawn's audit passes

    def _run(cmd, cwd, env=None):
        if "status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout=porcelain["out"], stderr="")
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        if "rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    mock_shell.run.side_effect = _run
    container.shell.override(mock_shell)

    def set_porcelain(s: str) -> None:
        porcelain["out"] = s

    yield workspace_with_git, set_porcelain
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def _spawn_bug(workspace: Path, slug_desc: str) -> None:
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    wi = items.create(title="cfg", kind="bug", workspace="ws", now=datetime.now(timezone.utc))
    result = runner.invoke(app, ["spawn", "--work-item", wi.id, slug_desc, "--repos", "shared"])
    assert result.exit_code == 0, result.output


def test_finish_not_blocked_by_config_only_dirty(cfg_only_workspace):
    workspace, set_porcelain = cfg_only_workspace
    _spawn_bug(workspace, "cfg only edit")           # spawn with a clean worktree
    set_porcelain(" M mothership.yaml\n")             # now config-only dirty for finish
    result = runner.invoke(app, ["finish", "--task", "cfg-only-edit"])
    assert result.exit_code == 0, result.output
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["cfg-only-edit"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"


def test_finish_reblocks_when_source_file_also_dirty(cfg_only_workspace):
    workspace, set_porcelain = cfg_only_workspace
    _spawn_bug(workspace, "cfg plus source")
    set_porcelain(" M mothership.yaml\n M src/app.py\n")
    result = runner.invoke(app, ["finish", "--task", "cfg-plus-source"])
    assert result.exit_code == 1, result.output
    assert "dirty" in result.output.lower()
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["cfg-plus-source"].pr_urls == {}
