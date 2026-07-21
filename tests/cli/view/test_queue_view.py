import pytest

from mship.core.view.queue import QueueItem
from mship.cli.view.queue import QueueView


def _items():
    return [
        QueueItem(kind="spec-needs-review", key="spec:wi-1", workspace="ws",
                  work_item_id="wi-1", work_item_title="Overhaul",
                  phase="shaping", spec_id="spec-1"),
        QueueItem(kind="blocked-task", key="block:a", workspace="ws",
                  work_item_id="wi-1", work_item_title="Overhaul",
                  phase="in_flight", task_slug="a",
                  blocked_reason="waiting on API key"),
        QueueItem(kind="pr-awaiting", key="pr:b:r", workspace="ws",
                  work_item_id="wi-1", work_item_title="Overhaul",
                  phase="review", task_slug="b", repo="r",
                  pr_url="https://gh/pr/9"),
    ]


@pytest.mark.asyncio
async def test_queue_view_lists_every_attention_item_with_header():
    view = QueueView(_items())
    async with view.run_test() as pilot:
        await pilot.pause()
        labels = view.list_labels()
        assert any("needs-review" in l for l in labels)
        assert any("blocked" in l for l in labels)
        assert any(l.startswith("[PR]") for l in labels)
        assert "queue" in view.header_text().lower()
        assert "3" in view.header_text()
        # First row (spec) detail shows the deferred-action note + spec id.
        assert "spec-1" in view.detail_text()


@pytest.mark.asyncio
async def test_queue_view_detail_follows_highlight():
    view = QueueView(_items())
    async with view.run_test() as pilot:
        await pilot.pause()
        view._master.focus()
        await pilot.pause()
        await pilot.press("j")  # -> blocked task
        await pilot.pause()
        assert "waiting on API key" in view.detail_text()
        await pilot.press("j")  # -> PR
        await pilot.pause()
        assert "https://gh/pr/9" in view.detail_text()


@pytest.mark.asyncio
async def test_queue_view_empty_is_safe():
    view = QueueView([])
    async with view.run_test() as pilot:
        await pilot.pause()
        assert view.list_labels() == []
        assert view.detail_text() == ""
        assert "0 needing attention" in view.header_text()


# --- CLI: mship view queue ---
from datetime import datetime, timezone

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.spec import Spec
from mship.core.spec_store import SPECS_DIRNAME, SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem import WorkItem
from mship.core.workitem_store import WorkItemStore


def _dt():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _seed_workspace(tmp_path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")

    # A needs_review spec (spec-1 / wi-1) + a blocked task and a PR task (wi-2).
    SpecStore(tmp_path / SPECS_DIRNAME).save(Spec(
        id="spec-1", title="Overhaul spec", status="needs_review",
        created_at=_dt(), updated_at=_dt(), body="b\n"))
    WorkItemStore(state_dir / "workitems").save(WorkItem(
        id="wi-1", title="Overhaul", workspace="t", kind="feature",
        created_at=_dt(), updated_at=_dt(), spec_id="spec-1"))
    WorkItemStore(state_dir / "workitems").save(WorkItem(
        id="wi-2", title="Wiring", workspace="t", kind="feature",
        created_at=_dt(), updated_at=_dt(), task_slugs=["a", "b"]))
    StateManager(state_dir).save(WorkspaceState(tasks={
        "a": Task(slug="a", description="d", phase="dev", created_at=_dt(),
                  affected_repos=["r"], branch="feat/a",
                  blocked_reason="waiting on API key", work_item_id="wi-2"),
        "b": Task(slug="b", description="d", phase="review", created_at=_dt(),
                  affected_repos=["r"], branch="feat/b",
                  pr_urls={"r": "https://gh/pr/9"}, finished_at=_dt(),
                  work_item_id="wi-2"),
    }))

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_queue_registered_in_view_help():
    result = CliRunner().invoke(app, ["view", "--help"])
    assert result.exit_code == 0
    assert "queue" in result.stdout


def test_queue_cli_renders_text_dump(tmp_path):
    _seed_workspace(tmp_path)
    try:
        # CliRunner stdout is not a TTY -> non-TTY text short-circuit (no TUI hang).
        result = CliRunner().invoke(app, ["view", "queue"])
        assert result.exit_code == 0, result.output
        assert "spec-1" in result.output           # spec needs review
        assert "waiting on API key" in result.output  # blocked task
        assert "https://gh/pr/9" in result.output     # PR awaiting
    finally:
        _reset()


def test_queue_cli_empty_workspace_is_ok(tmp_path):
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
        result = CliRunner().invoke(app, ["view", "queue"])
        assert result.exit_code == 0, result.output
        assert "0 needing attention" in result.output
        assert "(none)" in result.output
    finally:
        _reset()
