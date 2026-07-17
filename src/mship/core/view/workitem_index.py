from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from mship.core.message import Thread
from mship.core.spec import Spec
from mship.core.state import Task
from mship.core.workitem import ExternalLink, Kind, Phase, WorkItem

# MOS-240 status→phase projection (WorkItem.phase stays a projection, not
# authoritative). The collapsed `draft` maps to `shaping` — preserving the phase
# of every spec that could exist via the normal flow, since new specs were always
# created as `drafting` (→ shaping), never `captured`. `captured` was a vestigial
# status no code path produced; its old `inbox` mapping is not reachable post-shim
# (captured→draft on read). run_select only selects `ready`, so this choice leaves
# run-next selection unchanged either way.
_SPEC_PHASE: dict[str, Phase] = {
    "draft": "shaping",
    "needs_review": "shaping",
    "approved": "ready",
    "dispatched": "in_flight",
    "implemented": "done",
    "archived": "done",
}


_TERMINAL_SPEC_STATUSES: frozenset[str] = frozenset({"implemented", "archived"})


def compute_phase(item: WorkItem, spec: Spec | None, tasks: list[Task]) -> Phase:
    if item.phase_override is not None:
        return item.phase_override
    # Finding 2 (archived-spec-stays-active): a terminal spec status
    # (implemented/archived) is only ever reached at close/merge or an explicit
    # abandon, so it is authoritative — derive `done` BEFORE the task-state checks.
    # Otherwise an item whose spec is archived but whose linked task is still
    # unfinished would be wrongly held in_flight.
    if spec is not None and spec.status in _TERMINAL_SPEC_STATUSES:
        return "done"
    if tasks:
        # Any still-running task keeps the item in flight (unchanged derivation).
        if any(t.finished_at is None for t in tasks):
            return "in_flight"
        # Every linked task is finished. `mship finish` stamps finished_at (+ pr_urls)
        # when it OPENS the PR — not when it merges — so a finished task is awaiting
        # review/merge, NOT done. Map ANY finished task to `review`, whether or not a
        # PR URL is recorded: a finished no-PR task must not fall through to the
        # spec-derived `ready` phase, or run-next would re-select and run it a SECOND
        # time ("Finished Tasks Rerun"). `done` comes ONLY from a terminal spec
        # (checked above, set by `mship close`/merge); `finished_at` alone never
        # means done.
        #
        # NOTE: auto-advancing to `done` WITHOUT `mship close` — an operator who
        # merged the unattended PR directly, bypassing close — would need a merge
        # signal we don't have offline (the Task carries no `merged` flag). That's
        # a follow-up, out of scope here.
        return "review"
    if spec is not None:
        return _SPEC_PHASE.get(spec.status, "shaping")
    return "inbox"


@dataclass(frozen=True)
class Attention:
    needs_approval: bool
    needs_decision: bool
    blocked: bool
    needs_review: bool
    blocked_tasks: int
    total_tasks: int


def compute_attention(spec: Spec | None, tasks: list[Task], threads: list[Thread]) -> Attention:
    blocked_tasks = sum(1 for t in tasks if t.blocked_reason is not None)
    return Attention(
        needs_approval=spec is not None and spec.status == "needs_review",
        needs_decision=any(t.needs_you or t.needs_decision for t in threads),
        blocked=blocked_tasks > 0,
        needs_review=any(bool(t.pr_urls) for t in tasks),
        blocked_tasks=blocked_tasks,
        total_tasks=len(tasks),
    )


@dataclass(frozen=True)
class WorkItemSummary:
    id: str
    title: str
    kind: Kind
    workspace: str
    phase: str
    attention: Attention
    created_at: datetime
    updated_at: datetime
    spec_id: str | None
    task_slugs: list[str] = field(default_factory=list)
    thread_ids: list[str] = field(default_factory=list)
    external_links: list[ExternalLink] = field(default_factory=list)
    unattended: bool = False
    active_phase: str | None = None
    active_last_activity_at: datetime | None = None


def _active_task(tasks: list[Task]) -> Task | None:
    """The task an operator is watching. Among still-running (unfinished) tasks, prefer the one
    with the most recent activity — so a work item linked to several in-flight tasks reflects
    where work is actually happening — falling back to the first unfinished task when none have
    recorded activity yet. None when every linked task is finished."""
    unfinished = [t for t in tasks if t.finished_at is None]
    if not unfinished:
        return None
    active = [t for t in unfinished if t.last_activity_at is not None]
    if active:
        return max(active, key=lambda t: t.last_activity_at)
    return unfinished[0]


def _summarize(
    item: WorkItem,
    specs_by_id: dict[str, Spec],
    tasks_by_slug: dict[str, Task],
    threads_by_id: dict[str, Thread],
) -> WorkItemSummary:
    spec = specs_by_id.get(item.spec_id) if item.spec_id else None
    tasks = [tasks_by_slug[s] for s in item.task_slugs if s in tasks_by_slug]
    active = _active_task(tasks)
    threads = [threads_by_id[t] for t in item.thread_ids if t in threads_by_id]
    return WorkItemSummary(
        id=item.id, title=item.title, kind=item.kind, workspace=item.workspace,
        phase=compute_phase(item, spec, tasks),
        attention=compute_attention(spec, tasks, threads),
        created_at=item.created_at, updated_at=item.updated_at,
        spec_id=item.spec_id, task_slugs=list(item.task_slugs),
        thread_ids=list(item.thread_ids), external_links=list(item.external_links),
        unattended=item.unattended,
        active_phase=active.phase if active else None,
        active_last_activity_at=active.last_activity_at if active else None,
    )


def build_workitem_index(
    workitems: list[WorkItem],
    specs_by_id: dict[str, Spec],
    tasks_by_slug: dict[str, Task],
    threads_by_id: dict[str, Thread],
    include_archived: bool = False,
) -> list[WorkItemSummary]:
    """Non-done items first (updated_at desc), then done (also desc). Shared by serve + CLI.

    Archived items are excluded by default, mirroring WorkItemStore.list's own
    include_archived default (MOS-228 T3) — pass include_archived=True to include
    them (e.g. a caller that already fetched a specific item directly and wants it
    summarized regardless of its archived state)."""
    if not include_archived:
        workitems = [w for w in workitems if not w.archived]
    summaries = [_summarize(w, specs_by_id, tasks_by_slug, threads_by_id) for w in workitems]
    active = sorted([s for s in summaries if s.phase != "done"],
                    key=lambda s: s.updated_at, reverse=True)
    done = sorted([s for s in summaries if s.phase == "done"],
                  key=lambda s: s.updated_at, reverse=True)
    return active + done
