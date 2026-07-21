import pytest
from textual import work
from textual.app import App

from mship.cli.view._modals import EntityScreen, RequestChangesModal


class _Host(App):
    def __init__(self):
        super().__init__(); self.result = "unset"

    @work
    async def ask(self):
        self.result = await self.push_screen_wait(RequestChangesModal("spec-1"))


@pytest.mark.asyncio
async def test_reason_modal_returns_typed_reason():
    app = _Host()
    async with app.run_test() as pilot:
        app.ask()
        await pilot.pause()
        for ch in "fix ac2":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "fix ac2"


@pytest.mark.asyncio
async def test_reason_modal_escape_cancels():
    app = _Host()
    async with app.run_test() as pilot:
        app.ask()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None


@pytest.mark.asyncio
async def test_entity_screen_shows_text_and_dismisses():
    class H(App):
        def on_mount(self): self.push_screen(EntityScreen("spec-1", "SPEC BODY HERE"))
    app = H()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, EntityScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, EntityScreen)
