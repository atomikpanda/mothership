"""Shared TaskPicker: cross-task selection widget for view commands."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static

from mship.cli.view._base import ViewApp
from mship.core.view.task_index import TaskSummary


@dataclass(frozen=True)
class PickerRow:
    slug: str
    phase: str
    repos: str
    age: str
    flags: str
    extras: tuple[str, ...] = ()


def _age(created_at: datetime) -> str:
    delta = datetime.now(timezone.utc) - created_at
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _flags_for(summary: TaskSummary) -> str:
    parts: list[str] = []
    if summary.finished_at is not None:
        parts.append("⚠ close")
    if summary.blocked_reason:
        parts.append("🚫 blocked")
    if summary.tests_failing:
        parts.append("🧪 fail")
    if summary.orphan:
        parts.append("⚠ orphan")
    return " ".join(parts)


def picker_rows(
    index: Sequence[TaskSummary],
    extra: Callable[[TaskSummary], tuple[str, ...]] | None = None,
) -> list[PickerRow]:
    return [
        PickerRow(
            slug=s.slug,
            phase=s.phase,
            repos=",".join(s.affected_repos),
            age=_age(s.created_at),
            flags=_flags_for(s),
            extras=extra(s) if extra else (),
        )
        for s in index
    ]


class TaskPicker(ViewApp):
    """All-tasks picker. Subclasses (or callers) pass rows + an on_select callback."""

    BINDINGS = ViewApp.BINDINGS + [
        Binding("enter", "select_cursor", "Open", show=True),
    ]

    def __init__(
        self,
        rows: Sequence[PickerRow],
        extra_columns: Sequence[str] = (),
        on_select: Callable[[str], None] | None = None,
        **kw,
    ):
        super().__init__(**kw)
        self._rows = list(rows)
        self._extra_columns = tuple(extra_columns)
        self._on_select = on_select
        self._table: DataTable | None = None
        self._empty: Static | None = None

    def compose(self) -> ComposeResult:
        if not self._rows:
            self._empty = Static(
                "No tasks. Run `mship spawn \"…\"` to start one.", expand=True,
            )
            yield self._empty
            return
        self._table = DataTable(cursor_type="row")
        self._table.add_columns("slug", "phase", "repos", "age", "flags", *self._extra_columns)
        for r in self._rows:
            self._table.add_row(r.slug, r.phase, r.repos, r.age, r.flags, *r.extras, key=r.slug)
        yield self._table

    def on_mount(self) -> None:
        if self._table is not None:
            self._table.focus()

    def _refresh_content(self) -> None:
        # TaskPicker does not use ViewApp's Static/VerticalScroll body.
        return

    def on_data_table_row_selected(self, event) -> None:
        # DataTable consumes the Enter keypress, so we drive selection via its
        # RowSelected message instead of the app-level binding.
        self.action_select_cursor()

    def action_select_cursor(self) -> None:
        if self._table is None or self._on_select is None:
            return
        if self._table.cursor_row is None:
            return
        slug = self._rows[self._table.cursor_row].slug
        self._on_select(slug)
        self.exit()

    # Test helpers
    def row_slugs(self) -> list[str]:
        return [r.slug for r in self._rows]
