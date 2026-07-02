"""`mship finish` refuses to open a PR when the task has no WorkItem (or a
feature WorkItem without an approved spec). `--hotfix` downgrades the block to
a warning and records a bypass-log entry.

See core/workitem_gate.py::check_task_gate (task 1/6) and spec
workitem-mandatory-kind-gated-approval, task 4/6.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.spec import Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager
from mship.core.workitem_store import WorkItemStore
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def finish_gate_workspace(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    def _default_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
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
    mock_shell.run.side_effect = _default_run
    container.shell.override(mock_shell)

    yield workspace_with_git, mock_shell
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_finish_blocks_when_task_has_no_work_item(finish_gate_workspace):
    """A task spawned with --hotfix has work_item_id=None; finish must refuse."""
    workspace, _ = finish_gate_workspace
    result = runner.invoke(app, ["spawn", "--hotfix", "no workitem task", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--task", "no-workitem-task"])
    assert result.exit_code == 1, result.output
    assert "no WorkItem" in result.output

    # No PR should have been opened.
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["no-workitem-task"].pr_urls == {}


def test_finish_blocks_feature_work_item_without_approved_spec(finish_gate_workspace):
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    wi = items.create(title="add thing", kind="feature", workspace="ws",
                       now=datetime.now(timezone.utc))

    result = runner.invoke(
        app, ["spawn", "--work-item", wi.id, "feature task", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--task", "feature-task"])
    assert result.exit_code == 1, result.output
    assert "approved spec" in result.output

    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["feature-task"].pr_urls == {}


def test_finish_passes_with_bug_work_item_and_no_spec(finish_gate_workspace):
    """bug/chore/question WorkItems satisfy the gate without any spec at all."""
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    wi = items.create(title="fix it", kind="bug", workspace="ws",
                       now=datetime.now(timezone.utc))

    result = runner.invoke(
        app, ["spawn", "--work-item", wi.id, "bug task", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--task", "bug-task"])
    assert result.exit_code == 0, result.output

    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["bug-task"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"


def test_finish_passes_with_feature_work_item_and_approved_spec(finish_gate_workspace):
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    specs = SpecStore(workspace / "specs")
    now = datetime.now(timezone.utc)
    specs.save(Spec(id="spec-1", title="Spec", status="approved",
                    created_at=now, updated_at=now))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=now)
    items.link_spec(wi.id, "spec-1", now=now)

    result = runner.invoke(
        app, ["spawn", "--work-item", wi.id, "feature with spec", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--task", "feature-with-spec"])
    assert result.exit_code == 0, result.output

    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["feature-with-spec"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"


def test_finish_hotfix_warns_and_proceeds_and_logs_bypass(finish_gate_workspace):
    workspace, _ = finish_gate_workspace
    result = runner.invoke(app, ["spawn", "--hotfix", "hotfix finish task", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--hotfix", "--task", "hotfix-finish-task"])
    assert result.exit_code == 0, result.output
    assert "WorkItem gate bypassed" in result.output
    assert "--hotfix" in result.output

    # PR still gets opened — hotfix downgrades the block to a warning.
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["hotfix-finish-task"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"

    # Bypass is recorded to the shared bypass log.
    bypass_log = workspace / ".mothership" / "bypass-log.jsonl"
    assert bypass_log.is_file()
    lines = [json.loads(line) for line in bypass_log.read_text().splitlines()]
    assert any(
        entry["op"] == "finish" and entry["branch"] == "hotfix-finish-task" and entry["reason"] == "hotfix"
        for entry in lines
    ), lines
