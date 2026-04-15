"""Cross-task spec picker: rows of specs across every worktree."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Markdown, Static
from textual.containers import VerticalScroll

from mship.cli.view._base import ViewApp
from mship.core.view.task_index import SpecEntry, find_all_specs


def _fmt_mtime(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


class SpecIndexApp(ViewApp):
    BINDINGS = ViewApp.BINDINGS + [
        Binding("enter", "open_cursor", "Open", show=True),
        Binding("escape", "back_to_index", "Back", show=True),
    ]

    def __init__(self, workspace_root: Path, state, **kw):
        super().__init__(**kw)
        self._workspace_root = workspace_root
        self._state = state
        self._entries: list[SpecEntry] = []
        self._table: DataTable | None = None
        self._markdown: Markdown | None = None
        self._body: VerticalScroll | None = None
        self._empty: Static | None = None
        self._mode: str = "index"  # "index" | "spec"

    def compose(self) -> ComposeResult:
        self._entries = find_all_specs(self._state, self._workspace_root)
        if not self._entries:
            self._empty = Static("No specs found in any task or main checkout.", expand=True)
            yield self._empty
            return
        self._table = DataTable(cursor_type="row")
        self._table.add_columns("task", "filename", "modified", "title")
        for e in self._entries:
            slug = e.task_slug or "—"
            self._table.add_row(slug, e.path.name, _fmt_mtime(e.mtime), e.title, key=str(e.path))
        self._markdown = Markdown("")
        self._body = VerticalScroll(self._markdown)
        self._body.display = False
        yield self._table
        yield self._body

    def on_mount(self) -> None:
        if self._table is not None:
            self._table.focus()
        if self._watch:
            self.set_interval(self._interval, self._refresh_index)

    def _refresh_content(self) -> None:
        # SpecIndexApp manages its own table/markdown widgets; neutralize the
        # inherited ViewApp._refresh_content which assumes self._static exists.
        return

    def _refresh_index(self) -> None:
        if self._mode != "index" or self._table is None:
            return
        selected_key = None
        if self._table.cursor_row is not None and self._table.cursor_row < len(self._entries):
            selected_key = str(self._entries[self._table.cursor_row].path)
        self._entries = find_all_specs(self._state, self._workspace_root)
        self._table.clear()
        new_cursor = 0
        for i, e in enumerate(self._entries):
            slug = e.task_slug or "—"
            self._table.add_row(slug, e.path.name, _fmt_mtime(e.mtime), e.title, key=str(e.path))
            if str(e.path) == selected_key:
                new_cursor = i
        if self._entries:
            self._table.move_cursor(row=new_cursor)

    def action_open_cursor(self) -> None:
        if self._table is None or self._markdown is None or self._body is None:
            return
        if self._table.cursor_row is None or self._table.cursor_row >= len(self._entries):
            return
        entry = self._entries[self._table.cursor_row]
        try:
            self._markdown.update(entry.path.read_text())
        except OSError as e:
            self._markdown.update(f"Error reading spec: {e!r}")
        self._table.display = False
        self._body.display = True
        self._mode = "spec"
        self._body.focus()

    def action_back_to_index(self) -> None:
        if self._mode != "spec" or self._table is None or self._body is None:
            return
        self._body.display = False
        self._table.display = True
        self._mode = "index"
        self._table.focus()

    # Test helpers
    def row_filenames(self) -> list[str]:
        return [e.path.name for e in self._entries]

    def current_mode(self) -> str:
        return self._mode
