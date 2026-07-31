"""Tests for #447: `mship spec rejections [<id>] [--all]`.

Mirrors the `configured_app_with_task` real-container pattern used by the
request-changes tests in test_spec.py (rejections are recorded via
`record_rejection`, called from `spec request-changes`).
"""
from __future__ import annotations

import json
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
    container.log_manager.reset()
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
    container.log_manager.reset()


def _store(workspace: Path) -> SpecStore:
    return SpecStore(workspace / "specs")


def _draft_json() -> str:
    return json.dumps({
        "problem": "P", "user_story": "U", "approach": "A",
        "acceptance_criteria": ["view questions"], "open_questions": ["Android?"],
        "non_goals": ["chat"], "risks": [], "affected_repos": ["mothership"],
    })


def _apply(tmp_path: Path, spec_id: str):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", spec_id])
    jf = tmp_path / f"{spec_id}-draft.json"
    jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", spec_id, "--from-json", str(jf)])


def _reject(spec_id: str, reason: str):
    return runner.invoke(app, ["spec", "request-changes", spec_id, "--reason", reason])


def test_rejections_lists_records_for_a_spec(configured_app_with_task: Path, tmp_path):
    _apply(tmp_path, "dq")
    _reject("dq", "tighten scope")
    _apply(tmp_path, "dq")  # back to needs_review so a 2nd reject is legal
    _reject("dq", "still too broad")

    result = runner.invoke(app, ["spec", "rejections", "dq"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 2
    assert [r["reason"] for r in data] == ["tighten scope", "still too broad"]
    for r in data:
        assert "actor" in r
        assert "timestamp" in r
    # chronological
    timestamps = [r["timestamp"] for r in data]
    assert timestamps == sorted(timestamps)


def test_rejections_all_aggregates_across_specs(configured_app_with_task: Path, tmp_path):
    _apply(tmp_path, "dq")
    _reject("dq", "tighten scope")
    _apply(tmp_path, "other")
    _reject("other", "wrong approach")

    result = runner.invoke(app, ["spec", "rejections", "--all"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 2
    spec_ids = {r["spec_id"] for r in data}
    assert spec_ids == {"dq", "other"}
    reasons = {r["reason"] for r in data}
    assert reasons == {"tighten scope", "wrong approach"}


def test_rejections_skips_malformed_entry(configured_app_with_task: Path, tmp_path):
    _apply(tmp_path, "dq")
    _reject("dq", "tighten scope")

    # Hand-write a malformed action=rejected entry directly onto the log.
    log_manager = container.log_manager()
    log_manager.append("dq", "not json at all", action="rejected")

    result = runner.invoke(app, ["spec", "rejections", "dq"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # only the well-formed entry survives
    assert len(data) == 1
    assert data[0]["reason"] == "tighten scope"


def test_rejections_no_id_and_no_all_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "rejections"])
    assert result.exit_code != 0


def test_rejections_all_finds_deleted_spec(configured_app_with_task: Path, tmp_path):
    """#447 review: rejections are durable/append-only and must survive their
    spec being deleted — `--all` scans the log directory directly rather than
    filtering through `SpecStore.list()`."""
    _apply(tmp_path, "dq")
    _reject("dq", "tighten scope")

    # Delete the spec itself; the journal file (and its rejected entry) stays.
    store = _store(tmp_path)
    store.path_for(store.find_by_id("dq")).unlink()
    assert store.find_by_id("dq") is None

    result = runner.invoke(app, ["spec", "rejections", "--all"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [r["spec_id"] for r in data] == ["dq"]
    assert data[0]["reason"] == "tighten scope"


def test_rejections_all_is_time_sorted_across_specs(configured_app_with_task: Path, tmp_path):
    _apply(tmp_path, "dq")
    _reject("dq", "first")
    _apply(tmp_path, "other")
    _reject("other", "second")
    _apply(tmp_path, "dq")
    _reject("dq", "third")

    result = runner.invoke(app, ["spec", "rejections", "--all"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [r["reason"] for r in data] == ["first", "second", "third"]
    timestamps = [r["timestamp"] for r in data]
    assert timestamps == sorted(timestamps)
