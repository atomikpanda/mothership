"""Tests for `mship spec new` (#126)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


@pytest.fixture
def configured_app_with_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels to tasks",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))

    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def _blessed_path(workspace: Path, slug: str) -> Path:
    return workspace / ".mothership" / "tasks" / slug / "SPEC.md"


def test_spec_new_creates_blessed_file(configured_app_with_task: Path):
    """`mship spec new` scaffolds .mothership/tasks/<slug>/SPEC.md."""
    result = runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    assert result.exit_code == 0, result.output
    p = _blessed_path(configured_app_with_task, "add-labels")
    assert p.is_file()
    body = p.read_text()
    # Template includes task metadata so the file isn't blank.
    assert "add-labels" in body
    assert "Add labels to tasks" in body
    assert "shared" in body  # affected_repos
    assert "auth-service" in body


def test_spec_new_refuses_when_file_exists(configured_app_with_task: Path):
    """Without --force, refuse to overwrite an existing spec."""
    p = _blessed_path(configured_app_with_task, "add-labels")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# pre-existing content\n")

    result = runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    assert result.exit_code != 0, result.output
    assert "exists" in result.output.lower() or "already" in result.output.lower()
    # Original content untouched.
    assert p.read_text() == "# pre-existing content\n"


def test_spec_new_force_overwrites(configured_app_with_task: Path):
    """--force replaces existing content with the template."""
    p = _blessed_path(configured_app_with_task, "add-labels")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# pre-existing content\n")

    result = runner.invoke(app, ["spec", "new", "--task", "add-labels", "--force"])
    assert result.exit_code == 0, result.output
    body = p.read_text()
    assert "# pre-existing content" not in body
    assert "add-labels" in body


def test_spec_new_outputs_path_on_tty(configured_app_with_task: Path, monkeypatch):
    """TTY output includes the created path so the user can copy-paste."""
    from mship.cli.output import Output
    monkeypatch.setattr(Output, "is_tty", property(lambda self: True))
    result = runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    assert result.exit_code == 0, result.output
    p = _blessed_path(configured_app_with_task, "add-labels")
    # Rich may soft-wrap long paths; flatten before checking.
    flat = result.output.replace("\n", "").replace(" ", "")
    assert str(p) in flat


def test_spec_new_resolves_unknown_task(configured_app_with_task: Path):
    """An explicit unknown --task slug errors clearly."""
    result = runner.invoke(app, ["spec", "new", "--task", "nope"])
    assert result.exit_code != 0
    assert "nope" in result.output


# --- find_spec discovery of the blessed path (#126) ---


def test_find_spec_discovers_blessed_path_when_task_set(tmp_path: Path):
    """`mship view spec` (find_spec with task=<slug>) finds the blessed file."""
    from mship.core.state import Task, WorkspaceState
    from mship.core.view.spec_discovery import find_spec

    blessed = tmp_path / ".mothership" / "tasks" / "demo" / "SPEC.md"
    blessed.parent.mkdir(parents=True)
    blessed.write_text("# demo spec\n")

    task = Task(
        slug="demo",
        description="d",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["a"],
        branch="feat/demo",
    )
    state = WorkspaceState(tasks={"demo": task})
    found = find_spec(tmp_path, None, task="demo", state=state)
    assert found == blessed


# --- _gate_dev satisfaction by blessed path (#126) ---


def test_gate_dev_satisfied_by_blessed_path(tmp_path: Path):
    """`mship phase dev` doesn't warn when the task's blessed SPEC.md exists,
    even with no spec in the workspace-level docs/superpowers/specs dir."""
    from unittest.mock import MagicMock
    from mship.core.config import RepoConfig, WorkspaceConfig
    from mship.core.log import LogManager
    from mship.core.phase import PhaseManager
    from mship.core.state import StateManager, Task, WorkspaceState

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
        worktrees={"shared": tmp_path / "shared"},
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))

    # Place the blessed spec; nothing in docs/superpowers/specs.
    blessed = state_dir / "tasks" / "add-labels" / "SPEC.md"
    blessed.parent.mkdir(parents=True)
    blessed.write_text("# spec\n")

    config = WorkspaceConfig(
        workspace="t",
        repos={"shared": RepoConfig(path=Path("./shared"), type="library")},
    )
    pm = PhaseManager(
        mgr, MagicMock(spec=LogManager),
        config=config, workspace_root=tmp_path,
    )
    result = pm.transition("add-labels", "dev")
    assert not any("spec" in w.lower() for w in result.warnings), result.warnings


def test_gate_dev_hint_mentions_spec_new(tmp_path: Path):
    """The empty-workspace warning points at `mship spec new`."""
    from unittest.mock import MagicMock
    from mship.core.config import RepoConfig, WorkspaceConfig
    from mship.core.log import LogManager
    from mship.core.phase import PhaseManager
    from mship.core.state import StateManager, Task, WorkspaceState

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="d",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
        worktrees={"shared": tmp_path / "shared"},
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))
    config = WorkspaceConfig(
        workspace="t",
        repos={"shared": RepoConfig(path=Path("./shared"), type="library")},
    )
    pm = PhaseManager(
        mgr, MagicMock(spec=LogManager),
        config=config, workspace_root=tmp_path,
    )
    result = pm.transition("add-labels", "dev")
    spec_warn = next((w for w in result.warnings if "spec" in w.lower()), None)
    assert spec_warn is not None, result.warnings
    assert "mship spec new" in spec_warn
