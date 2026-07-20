"""Reusable lazygit-style master/detail Textual foundation for mship views.

A navigable list pane (left) beside a detail pane (right), a focus model (`tab`
toggles focus), list navigation (`j`/`k`/arrows move the highlight, `enter` drills
into the detail pane), an incremental filter (`/`), and a footer action bar that
renders the available keys.

Sibling to `ViewApp` (which stays the single-body `gather()` base for the stream
views status/journal/diff/spec, untouched here). This base is data-source
agnostic: subclasses implement `list_rows()` (and optionally `header_line()`); the
base owns compose, selection, focus, filtering, and the footer. Kept generic so
the future `queue` view reuses it unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static


@dataclass(frozen=True)
class ListRow:
    """One selectable row in the master list.

    `key` is a stable identifier (used by tests + future navigation), `label` is
    what shows in the list, `detail` is the pre-rendered text shown in the detail
    pane when the row is highlighted.
    """
    key: str
    label: str
    detail: str


class MasterDetailApp(App):
    CSS = """
    ListView#master {
        width: 40%;
        min-width: 24;
        border-right: tall $accent;
    }
    """

    # `tab` is priority so it beats Textual's built-in Screen `tab`->focus_next
    # binding; every other key stays non-priority so it can be typed into the
    # filter Input while that Input is focused.
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("r", "reload", "Refresh"),
        Binding("tab", "toggle_focus", "Switch pane", priority=True),
        Binding("slash", "start_filter", "Filter"),
        Binding("enter", "drill", "Open"),
        Binding("j,down", "nav_down", "Down", show=False),
        Binding("k,up", "nav_up", "Up", show=False),
        Binding("escape", "close_filter", "Close filter", show=False),
    ]

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._header: Static | None = None
        self._master: ListView | None = None
        self._detail_static: Static | None = None
        self._detail: VerticalScroll | None = None
        self._filter_input: Input | None = None
        self._filter: str = ""
        self._rows: list[ListRow] = []       # all rows (unfiltered)
        self._visible: list[ListRow] = []    # rows after the active filter

    # --- subclass hooks ---
    def list_rows(self) -> list[ListRow]:
        raise NotImplementedError

    def header_line(self) -> str | None:
        return None

    async def reload_rows(self) -> None:
        """Re-fetch rows on `r`. Default: rebuild from `list_rows()` (static
        snapshot). Subclasses backed by a live store override to re-query."""
        await self._rebuild()

    # --- lifecycle ---
    def compose(self) -> ComposeResult:
        self._header = Static("")
        self._master = ListView(id="master")
        self._detail_static = Static("", expand=True)
        self._detail = VerticalScroll(self._detail_static, id="detail")
        self._filter_input = Input(placeholder="filter (/ to focus)…", id="filter")
        yield self._header
        yield Horizontal(self._master, self._detail)
        yield self._filter_input
        yield Footer()

    async def on_mount(self) -> None:
        await self._rebuild()

    # --- rendering ---
    async def _rebuild(self) -> None:
        assert self._header is not None
        self._rows = list(self.list_rows())
        self._header.update(self.header_line() or "")
        await self._apply_filter()

    async def _apply_filter(self) -> None:
        assert self._master is not None
        needle = self._filter.strip().lower()
        self._visible = (
            [r for r in self._rows if needle in r.label.lower()] if needle
            else list(self._rows)
        )
        await self._master.clear()
        await self._master.extend([ListItem(Label(r.label)) for r in self._visible])
        # Textual's ListView starts with index=None (nothing highlighted) until a
        # cursor move; set it explicitly so the first row is genuinely highlighted
        # on mount and j/k/enter operate on a real cursor (not None).
        if self._visible:
            self._master.index = 0
        self.call_after_refresh(self._update_detail)

    def _current_index(self) -> int | None:
        assert self._master is not None
        if not self._visible:
            return None
        idx = self._master.index
        return idx if idx is not None and 0 <= idx < len(self._visible) else 0

    def _update_detail(self) -> None:
        assert self._detail_static is not None
        idx = self._current_index()
        self._detail_static.update("" if idx is None else self._visible[idx].detail)

    def on_list_view_highlighted(self, event) -> None:  # Textual: ListView.Highlighted
        self._update_detail()

    async def action_reload(self) -> None:
        await self.reload_rows()

    # --- focus model ---
    def _detail_focused(self) -> bool:
        return self._detail is not None and self._detail.has_focus

    def action_toggle_focus(self) -> None:
        if self._master is None or self._detail is None:
            return
        if self._detail_focused():
            self._master.focus()
        else:
            self._detail.focus()

    # --- navigation ---
    def action_nav_down(self) -> None:
        if self._detail_focused():
            assert self._detail is not None
            self._detail.scroll_relative(y=1, animate=False)
        elif self._master is not None:
            self._master.action_cursor_down()

    def action_nav_up(self) -> None:
        if self._detail_focused():
            assert self._detail is not None
            self._detail.scroll_relative(y=-1, animate=False)
        elif self._master is not None:
            self._master.action_cursor_up()

    def action_drill(self) -> None:
        # Enter drills into the highlighted entity: focus the detail pane so it
        # can be scrolled/read. (Cross-entity open/copy is a later PR.)
        if self._detail is not None:
            self._detail.focus()

    def on_list_view_selected(self, event) -> None:  # Textual: ListView.Selected (enter)
        self.action_drill()

    # --- test helpers ---
    _ANSI = re.compile(r"\x1b\[[0-9;]*[mKHFABCDJsu]")

    def list_labels(self) -> list[str]:
        return [r.label for r in self._visible]

    def detail_text(self) -> str:
        assert self._detail_static is not None
        return self._ANSI.sub("", str(self._detail_static.content))

    def header_text(self) -> str:
        assert self._header is not None
        return str(self._header.content)

    def selected_key(self) -> str | None:
        idx = self._current_index()
        return None if idx is None else self._visible[idx].key

    def focus_target(self) -> str:
        if self._filter_input is not None and self._filter_input.has_focus:
            return "filter"
        if self._detail_focused():
            return "detail"
        return "master"

    def detail_scroll_y(self) -> float:
        assert self._detail is not None
        return self._detail.scroll_y
