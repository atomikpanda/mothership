import pytest
from pathlib import Path

from mship.cli.view.status import StatusView


class _FakeTask:
    slug = "t1"
    phase = "impl"
    blocked_reason = None
    blocked_at = None
    branch = "feat/x"
    affected_repos = ["repo-a", "repo-b"]
    worktrees = {"repo-a": "/tmp/wta", "repo-b": "/tmp/wtb"}
    test_results = {}
    finished_at = None
    phase_entered_at = None


class _FakeState:
    current_task = "t1"
    tasks = {"t1": _FakeTask()}


class _FakeStateManager:
    def load(self):
        return _FakeState()


@pytest.mark.asyncio
async def test_status_view_renders_task():
    view = StatusView(state_manager=_FakeStateManager(), watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "t1" in text
        assert "impl" in text
        assert "repo-a" in text


@pytest.mark.asyncio
async def test_status_view_no_active_task():
    class _Empty:
        current_task = None
        tasks = {}

    class _Mgr:
        def load(self):
            return _Empty()

    view = StatusView(state_manager=_Mgr(), watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No active task" in view.rendered_text()


@pytest.mark.asyncio
async def test_status_view_shows_finished_warning():
    from datetime import datetime, timezone, timedelta

    class _Task:
        slug = "t"
        phase = "review"
        phase_entered_at = datetime.now(timezone.utc) - timedelta(hours=1)
        blocked_reason = None
        blocked_at = None
        branch = "feat/t"
        affected_repos = ["r"]
        worktrees = {}
        test_results = {}
        pr_urls = {}
        finished_at = datetime.now(timezone.utc) - timedelta(hours=2)

    class _State:
        current_task = "t"
        tasks = {"t": _Task()}

    class _Mgr:
        def load(self):
            return _State()

    view = StatusView(state_manager=_Mgr(), watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "Finished" in text or "finished" in text
        assert "mship close" in text


@pytest.mark.asyncio
async def test_status_view_shows_active_repo():
    from datetime import datetime, timezone

    class _Task:
        slug = "t"
        phase = "dev"
        phase_entered_at = datetime.now(timezone.utc)
        blocked_reason = None
        blocked_at = None
        branch = "feat/t"
        affected_repos = ["a", "b"]
        worktrees = {}
        test_results = {}
        pr_urls = {}
        finished_at = None
        active_repo = "a"

    class _State:
        current_task = "t"
        tasks = {"t": _Task()}

    class _Mgr:
        def load(self):
            return _State()

    view = StatusView(state_manager=_Mgr(), watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "Active repo" in text
        assert "a" in text
