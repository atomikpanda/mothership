from datetime import datetime, timezone

from mship.cli import container
from mship.core.spec import Spec
from mship.core.spec_store import SPECS_DIRNAME, SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem import WorkItem
from mship.core.workitem_store import WorkItemStore
from mship.cli.view._workitems import load_workitem_index


def _now():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _setup(tmp_path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")

    SpecStore(tmp_path / SPECS_DIRNAME).save(Spec(
        id="spec-1", title="Overhaul", status="approved",
        created_at=_now(), updated_at=_now(), body="b\n"))
    WorkItemStore(state_dir / "workitems").save(WorkItem(
        id="wi-1", title="Overhaul", workspace="t", kind="feature",
        created_at=_now(), updated_at=_now(), spec_id="spec-1", task_slugs=["a"]))
    StateManager(state_dir).save(WorkspaceState(tasks={"a": Task(
        slug="a", description="d", phase="dev", created_at=_now(),
        affected_repos=["r"], branch="feat/a", worktrees={}, work_item_id="wi-1")}))

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)


def _teardown():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_load_workitem_index_builds_from_canonical_stores(tmp_path):
    _setup(tmp_path)
    try:
        index = load_workitem_index(container)
        assert [s.id for s in index] == ["wi-1"]
        s = index[0]
        assert s.task_slugs == ["a"]
        assert s.spec_id == "spec-1"
        # approved spec + one unfinished task -> in_flight (build_workitem_index).
        assert s.phase == "in_flight"
    finally:
        _teardown()


def test_load_workitem_index_empty_workspace_is_empty(tmp_path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    StateManager(state_dir).save(WorkspaceState(tasks={}))
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        assert load_workitem_index(container) == []
    finally:
        _teardown()
