"""`mship view queue` — cross-workspace attention/triage list on the master/detail
base (AC4). Thin wiring: `build_rows` maps pure `QueueItem`s (assembled in
core/view/queue) to `ListRow`s, and `QueueView` renders them on the reusable
`MasterDetailApp`. READ-ONLY in this PR — navigate + view only.
"""
from __future__ import annotations

import typer

from mship.cli.view._master_detail import ListRow, MasterDetailApp
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
    def __init__(self, items: list[QueueItem], **kw) -> None:
        super().__init__(**kw)
        self._items = items

    def list_rows(self) -> list[ListRow]:
        return build_rows(self._items)

    def header_line(self) -> str | None:
        return queue_header(self._items)


def _resolve_queue(container) -> list[QueueItem]:
    """Assemble the queue from the canonical stores: the WorkItem summary index
    (PR1's load_workitem_index — carries the Attention rollup) + the workspace's
    tasks (blocked_reason + recorded pr_urls). No live gh call."""
    from mship.cli.view._workitems import load_workitem_index

    summaries = load_workitem_index(container)
    tasks = container.state_manager().load().tasks
    return assemble_queue(summaries, tasks)
