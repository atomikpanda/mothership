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
