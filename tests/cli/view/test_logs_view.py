import pytest
from dataclasses import dataclass
from datetime import datetime, timezone

from mship.cli.view.logs import LogsView


@dataclass
class _Entry:
    timestamp: datetime
    message: str
    repo: str | None = None
    iteration: int | None = None
    test_state: str | None = None
    action: str | None = None
    open_question: str | None = None


class _FakeLogMgr:
    def __init__(self, entries):
        self.entries = entries

    def read(self, slug, last=None):
        return list(self.entries)


class _FakeState:
    def __init__(self, slug):
        self.tasks = {slug: None} if slug else {}


class _FakeStateMgr:
    def __init__(self, slug="t1"):
        self._slug = slug

    def load(self):
        return _FakeState(self._slug)


@pytest.mark.asyncio
async def test_logs_view_renders_entries():
    entries = [
        _Entry(datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc), "hello"),
        _Entry(datetime(2026, 4, 13, 10, 5, tzinfo=timezone.utc), "world"),
    ]
    view = LogsView(
        state_manager=_FakeStateMgr(),
        log_manager=_FakeLogMgr(entries),
        task_slug="t1",
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "hello" in text
        assert "world" in text


@pytest.mark.asyncio
async def test_logs_view_no_task():
    view = LogsView(
        state_manager=_FakeStateMgr(slug=None),
        log_manager=_FakeLogMgr([]),
        task_slug=None,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No active task" in view.rendered_text()


@pytest.mark.asyncio
async def test_logs_view_explicit_slug():
    entries = [_Entry(datetime(2026, 4, 13, tzinfo=timezone.utc), "specific")]
    view = LogsView(
        state_manager=_FakeStateMgr(slug=None),
        log_manager=_FakeLogMgr(entries),
        task_slug="other-task",
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "specific" in view.rendered_text()


@pytest.mark.asyncio
async def test_logs_view_scopes_to_active_repo():
    from datetime import datetime, timezone

    entries = [
        _Entry(datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc), "shared thing", repo="shared"),
        _Entry(datetime(2026, 4, 14, 10, 5, tzinfo=timezone.utc), "cli thing", repo="cli"),
        _Entry(datetime(2026, 4, 14, 10, 6, tzinfo=timezone.utc), "untagged thing", repo=None),
    ]
    view = LogsView(
        state_manager=_FakeStateMgr(),
        log_manager=_FakeLogMgr(entries),
        task_slug="t1",
        scope_to_repo="cli",
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "cli thing" in text
        assert "shared thing" not in text
        # Untagged entries are kept (no repo tag to filter by)
        assert "untagged thing" in text


@pytest.mark.asyncio
async def test_logs_view_scope_none_shows_all():
    from datetime import datetime, timezone

    entries = [
        _Entry(datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc), "shared thing", repo="shared"),
        _Entry(datetime(2026, 4, 14, 10, 5, tzinfo=timezone.utc), "cli thing", repo="cli"),
    ]
    view = LogsView(
        state_manager=_FakeStateMgr(),
        log_manager=_FakeLogMgr(entries),
        task_slug="t1",
        scope_to_repo=None,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "cli thing" in text
        assert "shared thing" in text


# --- Task 6 additions ---

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, WorkspaceState


def test_logs_cli_rejects_unknown_task(tmp_path):
    runner = CliRunner()
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
        result = runner.invoke(app, ["view", "journal", "--task", "nope"])
        assert result.exit_code != 0
        assert "nope" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
        container.state_manager.reset_override()
        container.state_manager.reset()
