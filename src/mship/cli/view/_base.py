from __future__ import annotations

import re

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static


class ViewApp(App):
    """Base class for mship view TUIs.

    Subclasses override `gather()` to return the text body for the view.
    Watch-mode polls `gather()` on `interval` seconds and updates the body
    widget in place, preserving scroll position unless the user is pinned
    to the bottom (auto-follow).
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("r", "force_refresh", "Refresh"),
        Binding("j,down", "scroll_down", "Down", show=False),
        Binding("k,up", "scroll_up", "Up", show=False),
        Binding("pagedown", "page_down", "PgDn", show=False),
        Binding("pageup", "page_up", "PgUp", show=False),
        Binding("home", "scroll_home", "Home", show=False),
        Binding("end", "scroll_end", "End", show=False),
    ]

    def __init__(self, watch: bool = False, interval: float = 2.0, **kw) -> None:
        super().__init__(**kw)
        self._watch = watch
        self._interval = interval
        self._body: VerticalScroll | None = None
        self._static: Static | None = None

    # --- subclass hook ---
    def gather(self) -> str:
        raise NotImplementedError

    # --- Textual lifecycle ---
    def compose(self) -> ComposeResult:
        self._static = Static("", expand=True)
        self._body = VerticalScroll(self._static)
        yield self._body

    def on_mount(self) -> None:
        self._refresh_content()
        if self._watch:
            self.set_interval(self._interval, self._refresh_content)

    def _refresh_content(self) -> None:
        assert self._static is not None and self._body is not None
        was_at_end = self._body.scroll_y >= (self._body.max_scroll_y - 1)
        prev_y = self._body.scroll_y
        try:
            text = self.gather()
        except Exception as e:
            text = f"[error gathering content] {e!r}"
        self._static.update(Text.from_ansi(text))
        self.call_after_refresh(self._restore_scroll, prev_y, was_at_end)

    def _restore_scroll(self, prev_y: float, was_at_end: bool) -> None:
        assert self._body is not None
        if was_at_end:
            self._body.scroll_end(animate=False)
        else:
            self._body.set_scroll(x=None, y=prev_y)

    # --- actions ---
    def action_force_refresh(self) -> None:
        self._refresh_content()

    def action_scroll_down(self) -> None:
        assert self._body is not None
        self._body.scroll_relative(y=1, animate=False)

    def action_scroll_up(self) -> None:
        assert self._body is not None
        self._body.scroll_relative(y=-1, animate=False)

    def action_page_down(self) -> None:
        assert self._body is not None
        self._body.scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        assert self._body is not None
        self._body.scroll_page_up(animate=False)

    def action_scroll_home(self) -> None:
        assert self._body is not None
        self._body.scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        assert self._body is not None
        self._body.scroll_end(animate=False)

    # --- test helpers ---
    _ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[mKHFABCDJsu]")

    def rendered_text(self) -> str:
        assert self._static is not None
        return self._ANSI_ESCAPE.sub("", str(self._static.content))

    def body_scroll_y(self) -> float:
        assert self._body is not None
        return self._body.scroll_y

    def scroll_body_to(self, y: float) -> None:
        assert self._body is not None
        self._body.set_scroll(x=None, y=y)
