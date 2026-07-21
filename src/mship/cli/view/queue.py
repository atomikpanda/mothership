"""`mship view queue` — the workspace's attention/triage list on the master/detail
base (AC4). Thin wiring: `build_rows` maps pure `QueueItem`s (assembled in
core/view/queue) to `ListRow`s, and `QueueView` renders them on the reusable
`MasterDetailApp`. Scoped to THIS workspace (one serve/agent per workspace; the
cross-workspace rollup belongs to Ground Control).

PR4 adds the curated inline actions: `a` approves / `R` requests-changes a
spec-needs-review row (through the shared core.spec_transition seam, so the
terminal and the phone cannot diverge), `o` opens a PR url, `y` copies the row's
identity, and `enter` opens the linked spec in-process. Every other kind stays a
visible no-op.
"""
from __future__ import annotations

import webbrowser

import typer
from textual import work

from mship.cli.view._master_detail import ListRow, MasterDetailApp
from mship.cli.view._modals import EntityScreen, RequestChangesModal
from mship.core.view.actions import approve_spec_by_id, request_changes_by_id
from mship.core.view.queue import (
    QueueItem, assemble_queue, queue_detail, queue_header, queue_label,
)


def build_rows(items: list[QueueItem]) -> list[ListRow]:
    """One flat list of attention items — each row carrying its own pre-rendered
    detail (reusing the shared queue formatters)."""
    return [
        ListRow(key=i.key, label=queue_label(i), detail=queue_detail(i))
        for i in items
    ]


class QueueView(MasterDetailApp):
    def __init__(self, items: list[QueueItem], spec_store=None, **kw) -> None:
        super().__init__(**kw)
        self._items = list(items)
        self._spec_store = spec_store

    def list_rows(self) -> list[ListRow]:
        return build_rows(self._items)

    def header_line(self) -> str | None:
        return queue_header(self._items)

    def _selected(self) -> QueueItem | None:
        key = self.selected_key()
        return next((i for i in self._items if i.key == key), None)

    # AC7 — approve / request-changes (only on a spec-needs-review row) ------
    def _do_approve(self) -> None:
        item = self._selected()
        if item is None or item.kind != "spec-needs-review" or self._spec_store is None:
            self._announce("Approve applies to a spec awaiting review.")
            return
        out = approve_spec_by_id(self._spec_store, item.spec_id)
        self._announce(out.message)
        if out.ok:
            self._items = [i for i in self._items if i.key != item.key]
            self.call_later(self.reload_rows)

    @work
    async def _do_request_changes(self) -> None:
        item = self._selected()
        if item is None or item.kind != "spec-needs-review" or self._spec_store is None:
            self._announce("Request-changes applies to a spec awaiting review.")
            return
        reason = await self.push_screen_wait(RequestChangesModal(item.spec_id))
        if reason is None:
            self._announce("Request-changes cancelled.")
            return
        out = request_changes_by_id(self._spec_store, item.spec_id, reason)
        self._announce(out.message)
        if out.ok:
            self._items = [i for i in self._items if i.key != item.key]
            await self.reload_rows()

    # AC8 — open / copy -----------------------------------------------------
    def _do_open_external(self) -> None:
        item = self._selected()
        if item is not None and item.pr_url:
            webbrowser.open(item.pr_url)
            self._announce(f"Opened {item.pr_url}")
        else:
            self._announce("No PR/thread to open on this row.")

    def _do_copy(self) -> None:
        item = self._selected()
        text = None if item is None else (item.pr_url or item.spec_id or item.task_slug)
        if text:
            self.copy_to_clipboard(text)
            self._announce(f"Copied {text}")
        else:
            self._announce("Nothing to copy here.")

    def _do_open_entity(self) -> bool:
        # enter opens the linked entity for EVERY row type: a needs_review spec
        # opens its full body, a PR row opens in the browser, and any other row
        # (blocked task) opens its detail in a focused in-process screen.
        item = self._selected()
        if item is None:
            return False
        if item.kind == "spec-needs-review" and self._spec_store is not None:
            spec = self._spec_store.find_by_id(item.spec_id)
            if spec is not None:
                self.push_screen(EntityScreen(item.spec_id, spec.body))
                return True
        if item.pr_url:
            webbrowser.open(item.pr_url)
            self._announce(f"Opened {item.pr_url}")
            return True
        self.push_screen(EntityScreen(item.key, queue_detail(item)))
        return True


def _resolve_queue(container) -> list[QueueItem]:
    """Assemble the queue from the canonical stores: the WorkItem summary index
    (PR1's load_workitem_index — carries the Attention rollup) + the workspace's
    tasks (blocked_reason + recorded pr_urls). No live gh call."""
    from mship.cli.view._workitems import load_workitem_index

    summaries = load_workitem_index(container)
    tasks = container.state_manager().load().tasks
    return assemble_queue(summaries, tasks)


def register(app: "typer.Typer", get_container):
    @app.command()
    def queue():
        """This workspace's attention/triage queue: specs awaiting review, blocked
        tasks, and PRs awaiting action — each a navigable row with a detail pane.
        Curated inline actions: a approve · R request-changes · enter open · o
        open-in-browser · y copy."""
        from mship.cli.output import Output
        from mship.core.view.queue import render_text

        container = get_container()
        items = _resolve_queue(container)

        # Non-TTY short-circuit (mirrors `mship view workitem`): the Textual TUI
        # hangs when stdout isn't a terminal (agent pipes, CI, CliRunner). Print
        # the flat queue text and exit instead.
        if not Output().is_tty:
            typer.echo(render_text(items))
            return

        from pathlib import Path
        from mship.core.spec_store import SPECS_DIRNAME, SpecStore
        store = SpecStore(Path(container.config_path()).parent / SPECS_DIRNAME)
        QueueView(items, spec_store=store).run()
