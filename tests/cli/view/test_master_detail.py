import pytest

from mship.cli.view._master_detail import ListRow, MasterDetailApp


class _DemoView(MasterDetailApp):
    """Tiny concrete subclass so the generic base can be exercised in isolation."""

    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self._rows_src = list(rows)

    def list_rows(self):
        return self._rows_src

    def header_line(self):
        return "DEMO HEADER"


@pytest.mark.asyncio
async def test_list_populated_and_detail_follows_highlight():
    rows = [ListRow("a", "Alpha", "Detail A"), ListRow("b", "Bravo", "Detail B")]
    view = _DemoView(rows)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert view.list_labels() == ["Alpha", "Bravo"]
        assert view.header_text() == "DEMO HEADER"
        # First row is highlighted on mount; the detail pane shows its detail.
        assert view.selected_key() == "a"
        assert view.detail_text() == "Detail A"


@pytest.mark.asyncio
async def test_empty_rows_render_without_error():
    view = _DemoView([])
    async with view.run_test() as pilot:
        await pilot.pause()
        assert view.list_labels() == []
        assert view.selected_key() is None
        assert view.detail_text() == ""
