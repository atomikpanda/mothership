"""spawn gate: mship spawn requires a WorkItem (--work-item/--item), with a
--hotfix override. See spec workitem-mandatory-kind-gated-approval, task 2/6."""
import json
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


def _now():
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


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
def spawn_gate_workspace(workspace_with_git: Path):
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


def _items_store(workspace: Path) -> WorkItemStore:
    return WorkItemStore(workspace / ".mothership" / "workitems")


def test_spawn_without_work_item_or_hotfix_fails_and_creates_no_task(spawn_gate_workspace: Path):
    result = runner.invoke(app, ["spawn", "add labels", "--repos", "shared"])
    assert result.exit_code != 0
    state = StateManager(spawn_gate_workspace / ".mothership").load()
    assert "add-labels" not in state.tasks


def test_spawn_with_unknown_work_item_fails_and_creates_no_task(spawn_gate_workspace: Path):
    result = runner.invoke(
        app, ["spawn", "add labels", "--repos", "shared", "--work-item", "wi-nope"],
    )
    assert result.exit_code != 0
    state = StateManager(spawn_gate_workspace / ".mothership").load()
    assert "add-labels" not in state.tasks


def test_spawn_with_valid_work_item_sets_forward_and_reverse_link(spawn_gate_workspace: Path):
    items = _items_store(spawn_gate_workspace)
    wi = items.create(title="add labels", kind="chore", workspace="ws", now=_now())

    result = runner.invoke(
        app, ["spawn", "add labels", "--repos", "shared", "--work-item", wi.id],
    )
    assert result.exit_code == 0, result.output

    state = StateManager(spawn_gate_workspace / ".mothership").load()
    task = state.tasks["add-labels"]
    assert task.work_item_id == wi.id

    reloaded = items.get(wi.id)
    assert "add-labels" in reloaded.task_slugs


def test_spawn_feature_work_item_without_spec_or_plan_succeeds(spawn_gate_workspace: Path):
    """ac3: spawn is never spec/plan-gated — a FEATURE work item spawns fine
    before its spec is approved or its plan is written (those gate phase dev,
    not spawn)."""
    items = _items_store(spawn_gate_workspace)
    wi = items.create(title="add labels", kind="feature", workspace="ws", now=_now())

    result = runner.invoke(
        app, ["spawn", "add labels", "--repos", "shared", "--work-item", wi.id],
    )
    assert result.exit_code == 0, result.output
    state = StateManager(spawn_gate_workspace / ".mothership").load()
    assert state.tasks["add-labels"].work_item_id == wi.id


def test_spawn_with_item_alias_flag_accepted(spawn_gate_workspace: Path):
    """--item is the short alias for --work-item."""
    items = _items_store(spawn_gate_workspace)
    wi = items.create(title="add labels", kind="chore", workspace="ws", now=_now())

    result = runner.invoke(
        app, ["spawn", "add labels", "--repos", "shared", "--item", wi.id],
    )
    assert result.exit_code == 0, result.output
    state = StateManager(spawn_gate_workspace / ".mothership").load()
    assert state.tasks["add-labels"].work_item_id == wi.id


def test_spawn_hotfix_without_work_item_creates_task_and_logs_bypass(spawn_gate_workspace: Path):
    result = runner.invoke(
        app, ["spawn", "urgent fix", "--repos", "shared", "--hotfix"],
    )
    assert result.exit_code == 0, result.output

    state = StateManager(spawn_gate_workspace / ".mothership").load()
    task = state.tasks["urgent-fix"]
    assert task.work_item_id is None

    log_path = spawn_gate_workspace / ".mothership" / "bypass-log.jsonl"
    assert log_path.is_file()
    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(
        e.get("reason") == "hotfix" and e.get("op") == "spawn" and e.get("branch") == "urgent-fix"
        for e in entries
    )
