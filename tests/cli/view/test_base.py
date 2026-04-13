import pytest

from mship.cli.view._base import ViewApp


class _CountingView(ViewApp):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.renders = 0
        self.content = "line 1\nline 2\nline 3\n"

    def gather(self) -> str:
        self.renders += 1
        return self.content


@pytest.mark.asyncio
async def test_initial_render():
    app = _CountingView(watch=False, interval=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.renders == 1
        assert "line 1" in app.rendered_text()


@pytest.mark.asyncio
async def test_watch_mode_refreshes():
    app = _CountingView(watch=True, interval=0.05)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        assert app.renders >= 2


@pytest.mark.asyncio
async def test_quit_key():
    app = _CountingView(watch=False, interval=0.05)
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
        assert not app.is_running


@pytest.mark.asyncio
async def test_scroll_position_preserved_across_refresh():
    app = _CountingView(watch=True, interval=0.05)
    app.content = "\n".join(f"line {i}" for i in range(200)) + "\n"
    async with app.run_test() as pilot:
        await pilot.pause()
        app.scroll_body_to(50)
        y_before = app.body_scroll_y()
        app.content = "\n".join(f"line {i}" for i in range(201)) + "\n"  # grew
        await pilot.pause(0.15)
        assert app.body_scroll_y() == y_before, "should not yank when user scrolled away"


@pytest.mark.asyncio
async def test_auto_follow_when_pinned_to_bottom():
    app = _CountingView(watch=True, interval=0.05)
    app.content = "\n".join(f"line {i}" for i in range(200)) + "\n"
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scroll_end()
        await pilot.pause()
        y_end_before = app.body_scroll_y()
        app.content = "\n".join(f"line {i}" for i in range(400)) + "\n"
        await pilot.pause(0.15)
        assert app.body_scroll_y() >= y_end_before, "should auto-follow when pinned to bottom"
