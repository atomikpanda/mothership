from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from mship.core.message import Thread
from mship.core.spec import Spec
from mship.core.state import Task
from mship.core.workitem import ExternalLink, Kind, Phase, WorkItem

_SPEC_PHASE: dict[str, Phase] = {
    "captured": "inbox",
    "drafting": "shaping",
    "needs_review": "shaping",
    "needs_clarification": "shaping",
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
        # Finding 1 (premature-done): `mship finish` stamps finished_at + pr_urls
        # TOGETHER when it OPENS the PR — not when it merges — so a finished task
        # with a live, unmerged PR is awaiting review, not done. `done` comes ONLY
        # from a terminal spec (checked above, set by `mship close`/merge);
        # `finished_at` alone never means done.
        #
        # NOTE: auto-advancing to `done` WITHOUT `mship close` — an operator who
        # merged the unattended PR directly, bypassing close — would need a merge
        # signal we don't have offline (the Task carries no `merged` flag). That's
        # a follow-up, out of scope here.
        if any(t.pr_urls for t in tasks):
            return "review"
        # Finished but no PR recorded: still not `done` (done is spec-driven).
        # Fall through to the spec-derived phase below.
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


def _summarize(
    item: WorkItem,
    specs_by_id: dict[str, Spec],
    tasks_by_slug: dict[str, Task],
    threads_by_id: dict[str, Thread],
) -> WorkItemSummary:
    spec = specs_by_id.get(item.spec_id) if item.spec_id else None
    tasks = [tasks_by_slug[s] for s in item.task_slugs if s in tasks_by_slug]
    threads = [threads_by_id[t] for t in item.thread_ids if t in threads_by_id]
    return WorkItemSummary(
        id=item.id, title=item.title, kind=item.kind, workspace=item.workspace,
        phase=compute_phase(item, spec, tasks),
        attention=compute_attention(spec, tasks, threads),
        created_at=item.created_at, updated_at=item.updated_at,
        spec_id=item.spec_id, task_slugs=list(item.task_slugs),
        thread_ids=list(item.thread_ids), external_links=list(item.external_links),
        unattended=item.unattended,
    )


def build_workitem_index(
    workitems: list[WorkItem],
    specs_by_id: dict[str, Spec],
    tasks_by_slug: dict[str, Task],
    threads_by_id: dict[str, Thread],
) -> list[WorkItemSummary]:
    """Non-done items first (updated_at desc), then done (also desc). Shared by serve + CLI."""
    summaries = [_summarize(w, specs_by_id, tasks_by_slug, threads_by_id) for w in workitems]
    active = sorted([s for s in summaries if s.phase != "done"],
                    key=lambda s: s.updated_at, reverse=True)
    done = sorted([s for s in summaries if s.phase == "done"],
                  key=lambda s: s.updated_at, reverse=True)
    return active + done
