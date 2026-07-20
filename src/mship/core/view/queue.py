"""Pure assembly of the `mship view queue` attention/triage list (AC4).

Folds the WorkItem summary index (which already carries the canonical `Attention`
rollup from `compute_attention`) plus the workspace's `Task`s into a flat,
render-ready list of attention items: specs awaiting review, blocked tasks, and
PRs awaiting action. No Textual, no container, no store I/O — unit-testable
directly. The Textual `QueueView` and the CLI command wire thin on top; the
formatters below are shared by both the flat text renderer (non-TTY) and the TUI
row builder (mirrors workitem_cockpit's split).

READ-ONLY: PR-awaiting rows come from each task's RECORDED `pr_urls` (stamped by
`mship finish` at PR-open time), never a live `gh` call — live PR state is only
ever resolved by `mship serve`'s PrWatcher, never in a view. Completed work
(merged/closed PRs, implemented specs) drops off via the `phase != "done"` gate.
"""
from __future__ import annotations

from dataclasses import dataclass

from mship.core.state import Task
from mship.core.view.workitem_index import WorkItemSummary


@dataclass(frozen=True)
class QueueItem:
    """One attention item. `kind` is one of "spec-needs-review", "blocked-task",
    "pr-awaiting". `key` is stable + unique across the queue (ListRow.key). Every
    item carries its owning WorkItem context (id/title/phase/workspace); the
    kind-specific fields below are populated per kind."""
    kind: str
    key: str
    workspace: str
    work_item_id: str
    work_item_title: str
    phase: str
    spec_id: str | None = None
    task_slug: str | None = None
    blocked_reason: str | None = None
    repo: str | None = None
    pr_url: str | None = None


def assemble_queue(
    summaries: list[WorkItemSummary],
    tasks_by_slug: dict[str, Task],
) -> list[QueueItem]:
    """Fold the WorkItem index + tasks into the flat attention list (AC4).

    Grouped by kind (specs → blocked tasks → PRs); within a kind, WorkItem order
    is preserved (the index is already updated_at-desc). Done work items are
    skipped: a merged+closed PR / implemented spec derives `phase == "done"` and
    is no longer awaiting a human.
    """
    specs: list[QueueItem] = []
    for s in summaries:
        if s.phase == "done":
            continue
        if s.attention.needs_approval and s.spec_id is not None:
            specs.append(QueueItem(
                kind="spec-needs-review", key=f"spec:{s.id}",
                workspace=s.workspace, work_item_id=s.id,
                work_item_title=s.title, phase=s.phase, spec_id=s.spec_id,
            ))
    return specs
