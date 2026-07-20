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


@pytest.mark.asyncio
async def test_tab_toggles_focus_between_panes():
    view = _DemoView([ListRow("a", "Alpha", "Detail A")])
    async with view.run_test() as pilot:
        await pilot.pause()
        view._master.focus()
        await pilot.pause()
        assert view.focus_target() == "master"
        await pilot.press("tab")
        await pilot.pause()
        assert view.focus_target() == "detail"
        await pilot.press("tab")
        await pilot.pause()
        assert view.focus_target() == "master"


@pytest.mark.asyncio
async def test_j_k_move_selection_when_master_focused():
    rows = [ListRow("a", "Alpha", "dA"), ListRow("b", "Bravo", "dB"),
            ListRow("c", "Cara", "dC")]
    view = _DemoView(rows)
    async with view.run_test() as pilot:
        await pilot.pause()
        view._master.focus()
        await pilot.pause()
        assert view.selected_key() == "a"
        await pilot.press("j")
        await pilot.pause()
        assert view.selected_key() == "b"
        assert view.detail_text() == "dB"
        await pilot.press("k")
        await pilot.pause()
        assert view.selected_key() == "a"
        assert view.detail_text() == "dA"


@pytest.mark.asyncio
async def test_enter_drills_into_detail():
    view = _DemoView([ListRow("a", "Alpha", "dA")])
    async with view.run_test() as pilot:
        await pilot.pause()
        view._master.focus()
        await pilot.pause()
        assert view.focus_target() == "master"
        await pilot.press("enter")
        await pilot.pause()
        assert view.focus_target() == "detail"


@pytest.mark.asyncio
async def test_j_scrolls_detail_when_detail_focused():
    long_detail = "\n".join(f"line {i}" for i in range(200))
    view = _DemoView([ListRow("a", "Alpha", long_detail)])
    async with view.run_test() as pilot:
        await pilot.pause()
        await pilot.press("tab")  # focus the detail pane
        await pilot.pause()
        assert view.focus_target() == "detail"
        assert view.detail_scroll_y() == 0
        for _ in range(5):
            await pilot.press("j")
        await pilot.pause()
        assert view.detail_scroll_y() > 0


@pytest.mark.asyncio
async def test_slash_filters_list_incrementally():
    rows = [ListRow("a", "Alpha", "dA"), ListRow("b", "Bravo", "dB"),
            ListRow("c", "Alpaca", "dC")]
    view = _DemoView(rows)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert len(view.list_labels()) == 3
        await pilot.press("slash")
        await pilot.pause()
        assert view.focus_target() == "filter"
        for ch in "alp":
            await pilot.press(ch)
        await pilot.pause()
        # "alp" (case-insensitive) matches Alpha and Alpaca, not Bravo.
        assert set(view.list_labels()) == {"Alpha", "Alpaca"}
        await pilot.press("enter")  # submit closes the filter, refocuses the list
        await pilot.pause()
        assert view.focus_target() == "master"
        # Filter text persists; list stays filtered.
        assert set(view.list_labels()) == {"Alpha", "Alpaca"}


@pytest.mark.asyncio
async def test_escape_closes_filter_and_refocuses_master():
    view = _DemoView([ListRow("a", "Alpha", "dA")])
    async with view.run_test() as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        assert view.focus_target() == "filter"
        await pilot.press("escape")
        await pilot.pause()
        assert view.focus_target() == "master"


@pytest.mark.asyncio
async def test_rich_markup_in_canonical_text_is_shown_literally():
    # Greptile #391: labels/detail/header come from user-authored canonical records
    # and may contain Rich syntax like "[red]" or malformed "[". markup=False must
    # render them literally without interpreting or crashing.
    rows = [ListRow("a", "ac1 [red]danger[/red] text", "detail with [bold]x[/] and [oops")]
    view = _DemoView(rows)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert view.list_labels() == ["ac1 [red]danger[/red] text"]
        assert "[bold]x[/]" in view.detail_text()
        assert "[oops" in view.detail_text()   # malformed markup did not crash render
