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
    blocked: list[QueueItem] = []
    prs: list[QueueItem] = []
    for s in summaries:
        if s.phase == "done":
            continue
        if s.attention.needs_approval and s.spec_id is not None:
            specs.append(QueueItem(
                kind="spec-needs-review", key=f"spec:{s.id}",
                workspace=s.workspace, work_item_id=s.id,
                work_item_title=s.title, phase=s.phase, spec_id=s.spec_id,
            ))
        for slug in s.task_slugs:
            task = tasks_by_slug.get(slug)
            if task is None:
                continue
            if task.blocked_reason is not None:
                blocked.append(QueueItem(
                    kind="blocked-task", key=f"block:{slug}",
                    workspace=s.workspace, work_item_id=s.id,
                    work_item_title=s.title, phase=s.phase,
                    task_slug=slug, blocked_reason=task.blocked_reason,
                ))
            for repo, url in task.pr_urls.items():
                prs.append(QueueItem(
                    kind="pr-awaiting", key=f"pr:{slug}:{repo}",
                    workspace=s.workspace, work_item_id=s.id,
                    work_item_title=s.title, phase=s.phase,
                    task_slug=slug, repo=repo, pr_url=url,
                ))
    return specs + blocked + prs


# --- formatters (shared by render_text + the TUI row builder) ---

# The read-only note surfaced in detail panes for items that will grow inline
# actions in a later PR (approve / request-changes — AC7).
_ACTION_DEFERRED = "  action: approve / request-changes (deferred — read-only in this view)"


def _context_lines(item: QueueItem) -> list[str]:
    return [
        f"  work item: {item.work_item_id}  ·  {item.work_item_title}  [{item.phase}]",
        f"  workspace: {item.workspace}",
    ]


def queue_label(item: QueueItem) -> str:
    if item.kind == "spec-needs-review":
        return f"[needs-review]  {item.spec_id}  ·  {item.work_item_title}"
    if item.kind == "blocked-task":
        return f"[blocked]  {item.task_slug}  —  {item.blocked_reason}"
    return f"[PR]  {item.repo}  ({item.task_slug})"


def queue_detail(item: QueueItem) -> str:
    if item.kind == "spec-needs-review":
        lines = [f"spec {item.spec_id}  [needs_review]", f"  {item.work_item_title}"]
        lines += _context_lines(item)
        lines.append(_ACTION_DEFERRED)
        return "\n".join(lines)
    if item.kind == "blocked-task":
        lines = [f"task {item.task_slug}  [BLOCKED]", f"  reason: {item.blocked_reason}"]
        lines += _context_lines(item)
        return "\n".join(lines)
    lines = [f"PR ({item.repo}, task {item.task_slug})", f"  {item.pr_url}"]
    lines += _context_lines(item)
    lines.append(_ACTION_DEFERRED)
    return "\n".join(lines)


def queue_header(items: list[QueueItem]) -> str:
    n_spec = sum(1 for i in items if i.kind == "spec-needs-review")
    n_block = sum(1 for i in items if i.kind == "blocked-task")
    n_pr = sum(1 for i in items if i.kind == "pr-awaiting")
    return (
        f"◆ queue  ·  {len(items)} needing attention  ·  "
        f"{n_spec} specs · {n_block} blocked · {n_pr} PRs"
    )


def render_text(items: list[QueueItem]) -> str:
    """Flat text dump of the whole queue — the non-TTY short-circuit output
    (agent pipes / CI), mirroring workitem_cockpit.render_text."""
    parts: list[str] = [queue_header(items), ""]
    for title, kind in (
        ("SPECS NEEDS REVIEW", "spec-needs-review"),
        ("BLOCKED TASKS", "blocked-task"),
        ("PRS AWAITING ACTION", "pr-awaiting"),
    ):
        parts.append(title)
        section = [i for i in items if i.kind == kind]
        parts.extend(queue_detail(i) for i in section)
        if not section:
            parts.append("(none)")
        parts.append("")
    return "\n".join(parts).rstrip("\n")
