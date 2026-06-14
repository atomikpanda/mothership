"""Tests for `mship spec new` (#126, #145)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.spec_store import SpecStore
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


def _store(workspace: Path) -> SpecStore:
    return SpecStore(workspace / "specs")


def test_spec_new_creates_structured_file(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--title", "Add labels"])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("add-labels")
    assert spec is not None
    assert spec.status == "drafting"
    assert spec.title == "Add labels"
    assert "## Problem" in spec.body


def test_spec_new_with_task_prefills_repos_and_binds(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("add-labels")
    assert spec is not None
    assert spec.task_slug == "add-labels"
    assert spec.affected_repos == ["shared", "auth-service"]
    assert spec.title == "Add labels to tasks"


def test_spec_new_requires_title_or_task(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new"])
    assert result.exit_code != 0
    assert "title" in result.output.lower()


def test_spec_new_refuses_existing(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Add labels"])
    result = runner.invoke(app, ["spec", "new", "--title", "Add labels"])
    assert result.exit_code != 0
    assert "exists" in result.output.lower() or "already" in result.output.lower()


def test_spec_new_force_overwrites(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Add labels"])
    result = runner.invoke(app, ["spec", "new", "--title", "Add labels", "--force"])
    assert result.exit_code == 0, result.output


def test_spec_new_unknown_task_errors(configured_app_with_task: Path):
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


def test_spec_new_id_override(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--title", "Something", "--id", "my-id"])
    assert result.exit_code == 0, result.output
    assert _store(configured_app_with_task).find_by_id("my-id") is not None


def test_spec_new_title_overrides_task_description(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--task", "add-labels", "--title", "Override"])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("add-labels")
    assert spec.title == "Override"          # explicit title wins over task.description
    assert spec.task_slug == "add-labels"    # still bound + prefilled


def test_spec_new_empty_title_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--title", ""])
    assert result.exit_code != 0


def test_spec_new_json_output_non_tty(configured_app_with_task: Path, monkeypatch):
    import json
    from mship.cli.output import Output
    monkeypatch.setattr(Output, "is_tty", property(lambda self: False))
    result = runner.invoke(app, ["spec", "new", "--title", "Json Spec"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == "json-spec"
    assert payload["status"] == "drafting"
    assert "path" in payload


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


def test_spec_draft_emits_prompt(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    result = runner.invoke(app, ["spec", "draft", "dq", "--from-text", "rambled intent here"])
    assert result.exit_code == 0, result.output
    assert "rambled intent here" in result.output
    assert "mship spec apply dq --from-json" in result.output


def test_spec_draft_requires_exactly_one_source(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    neither = runner.invoke(app, ["spec", "draft", "dq"])
    assert neither.exit_code != 0
    both = runner.invoke(app, ["spec", "draft", "dq", "--from-text", "x", "--from-file", "f.md"])
    assert both.exit_code != 0


def test_spec_draft_unknown_id_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "draft", "nope", "--from-text", "x"])
    assert result.exit_code != 0
    assert "nope" in result.output


# --- spec apply (#146) ---

import json as _json


def _draft_json() -> str:
    return _json.dumps({
        "problem": "P", "user_story": "U", "approach": "A",
        "acceptance_criteria": ["view questions"], "open_questions": ["Android?"],
        "non_goals": ["chat"], "risks": [], "affected_repos": ["mothership"],
    })


def test_spec_apply_merges_and_advances_status(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"
    jf.write_text(_draft_json())
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.status == "needs_review"
    assert [c.id for c in spec.acceptance_criteria] == ["ac1"]
    assert "## Problem" in spec.body


def test_spec_apply_rejects_invalid_json(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "bad.json"
    jf.write_text('{"problem": "only problem"}')   # missing required fields
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    assert result.exit_code != 0


def test_spec_apply_refuses_wrong_status(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"; jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])           # -> needs_review
    again = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])    # needs_review->needs_review illegal
    assert again.exit_code != 0
    forced = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf), "--bypass-status-gate"])
    assert forced.exit_code == 0, forced.output


# --- spec validate (#146) ---


def test_spec_validate_passes_on_applied_spec(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"; jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    result = runner.invoke(app, ["spec", "validate", "dq"])
    assert result.exit_code == 0, result.output


def test_spec_validate_flags_missing_section(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    store = _store(configured_app_with_task)
    spec = store.find_by_id("dq")
    spec.body = "## Problem\n\njust the problem\n"   # drop User story + Approach
    store.save(spec)
    result = runner.invoke(app, ["spec", "validate", "dq"])
    assert result.exit_code != 0
    assert "User story" in result.output or "Approach" in result.output


def test_spec_validate_unknown_id_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "validate", "nope"])
    assert result.exit_code != 0


def test_spec_apply_rejects_malformed_json(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "broken.json"
    jf.write_text("this is not json at all")        # JSONDecodeError branch
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    assert result.exit_code != 0


def test_spec_apply_reads_stdin(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    result = runner.invoke(
        app, ["spec", "apply", "dq", "--from-json", "-"], input=_draft_json()
    )
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.status == "needs_review"
