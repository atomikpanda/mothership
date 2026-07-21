from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.spec import Spec
from mship.core.spec_store import SPECS_DIRNAME, SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem import WorkItem
from mship.core.workitem_store import WorkItemStore


def _now():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _seed(tmp_path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    SpecStore(tmp_path / SPECS_DIRNAME).save(Spec(
        id="spec-1", title="Overhaul", status="approved",
        created_at=_now(), updated_at=_now(), body="b\n"))
    WorkItemStore(state_dir / "workitems").save(WorkItem(
        id="wi-1", title="Overhaul", workspace="t", kind="feature",
        created_at=_now(), updated_at=_now(), spec_id="spec-1", task_slugs=["a"]))
    StateManager(state_dir).save(WorkspaceState(tasks={"a": Task(
        slug="a", description="d", phase="dev", created_at=_now(),
        affected_repos=["r"], branch="feat/a", worktrees={}, work_item_id="wi-1")}))
    container.config.reset(); container.state_manager.reset()
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)


def _reset():
    container.config_path.reset_override(); container.state_dir.reset_override()
    container.config.reset_override(); container.config.reset()
    container.state_manager.reset_override(); container.state_manager.reset()


def test_items_registered_in_view_help():
    result = CliRunner().invoke(app, ["view", "--help"])
    assert result.exit_code == 0
    assert "items" in result.stdout


def test_items_cli_renders_text(tmp_path):
    _seed(tmp_path)
    try:
        result = CliRunner().invoke(app, ["view", "items"])
        assert result.exit_code == 0, result.output
        assert "wi-1" in result.output and "Overhaul" in result.output
    finally:
        _reset()


@pytest.mark.asyncio
async def test_items_view_lists_and_enter_focuses(tmp_path, monkeypatch):
    from mship.core.view.workitem_index import Attention, WorkItemSummary
    import mship.cli.view.items as iv

    fired = {}
    monkeypatch.setattr(iv, "_focus_workitem", lambda item_id: fired.setdefault("id", item_id))
    s = WorkItemSummary(
        id="wi-1", title="Overhaul", kind="feature", workspace="t", phase="in_flight",
        attention=Attention(False, False, False, False, 0, 1),
        created_at=_now(), updated_at=_now(), spec_id="spec-1", task_slugs=["a"], thread_ids=[])
    view = iv.ItemsView([s])
    async with view.run_test() as pilot:
        await pilot.pause()
        assert any("wi-1" in l for l in view.list_labels())
        view._master.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert fired.get("id") == "wi-1"


@pytest.mark.asyncio
async def test_items_view_shows_enter_and_copy_hint():
    # The operator kept not realizing enter opens the tab — the hint must be
    # visible on-screen, not just in the command docstring.
    from mship.core.view.workitem_index import Attention, WorkItemSummary
    import mship.cli.view.items as iv

    s = WorkItemSummary(
        id="wi-1", title="Overhaul", kind="feature", workspace="t", phase="in_flight",
        attention=Attention(False, False, False, False, 0, 1),
        created_at=_now(), updated_at=_now(), spec_id="spec-1", task_slugs=["a"], thread_ids=[])
    view = iv.ItemsView([s])
    async with view.run_test() as pilot:
        await pilot.pause()
        header = view.header_text()
        assert "enter" in header.lower()
        assert "copy" in header.lower()


@pytest.mark.asyncio
async def test_items_enter_announces_focus_failure(monkeypatch):
    # Greptile #396: a failed `mship layout focus` must not report success.
    from mship.core.view.workitem_index import Attention, WorkItemSummary
    import mship.cli.view.items as iv

    monkeypatch.setattr(iv, "_focus_workitem", lambda item_id: False)
    s = WorkItemSummary(
        id="wi-1", title="X", kind="feature", workspace="t", phase="in_flight",
        attention=Attention(False, False, False, False, 0, 1),
        created_at=_now(), updated_at=_now(), spec_id=None, task_slugs=[], thread_ids=[])
    view = iv.ItemsView([s])
    async with view.run_test() as pilot:
        await pilot.pause()
        view._master.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "could not focus" in view.last_action().lower()
