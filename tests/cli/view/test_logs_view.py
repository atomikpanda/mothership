import pytest
from dataclasses import dataclass
from datetime import datetime, timezone

from mship.cli.view.logs import LogsView


@dataclass
class _Entry:
    timestamp: datetime
    message: str


class _FakeLogMgr:
    def __init__(self, entries):
        self.entries = entries

    def read(self, slug, last=None):
        return list(self.entries)


class _FakeState:
    def __init__(self, slug):
        self.current_task = slug
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
        task_slug=None,
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
