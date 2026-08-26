"""Read-time resolution of a thread's related WorkItem (inverts the WorkItem link graph)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable

from mship.core.view.workitem_index import compute_phase


@dataclass(frozen=True)
class ThreadInboxLink:
    work_item_id: str | None
    terminal: bool
    uncertain: bool = False


def index_thread_inbox_links(
    threads: Iterable,
    items: Iterable,
    specs_by_id: dict,
    tasks_by_slug: dict,
    *,
    uncertain: bool = False,
    ambiguous_spec_ids: frozenset[str] = frozenset(),
) -> dict[str, ThreadInboxLink]:
    """Resolve each thread's WorkItem once and derive whether that owner is done."""
    try:
        item_list = list(items)
        index = _build_link_index(item_list)
        items_by_id = {item.id: item for item in item_list}
    except Exception:
        return {thread.id: ThreadInboxLink(None, False) for thread in threads}

    links: dict[str, ThreadInboxLink] = {}
    for thread in threads:
        try:
            work_item_id = _resolve_from_index(
                thread.id, thread.spec_id, thread.task_slug, index,
            )
            item = items_by_id.get(work_item_id)
            ambiguous_spec = item is not None and item.spec_id in ambiguous_spec_ids
            terminal = not ambiguous_spec and item is not None and compute_phase(
                item,
                specs_by_id.get(item.spec_id) if item.spec_id else None,
                [tasks_by_slug[slug] for slug in item.task_slugs if slug in tasks_by_slug],
            ) == "done"
            links[thread.id] = ThreadInboxLink(
                work_item_id,
                terminal,
                ambiguous_spec
                or (uncertain and work_item_id is None)
                or (work_item_id is None and bool(thread.spec_id or thread.task_slug)),
            )
        except Exception:
            links[thread.id] = ThreadInboxLink(None, False, True)
    return links

def _build_link_index(items: Iterable):
    """Build the three reverse lookups (thread_id / spec_id / task_slug -> work_item_id)
    once, so a whole thread list can be resolved without re-scanning `items` per thread."""
    # Per-item guard: a single corrupt/unreadable WorkItem degrades ONLY its own threads (they fall
    # back to None), not every thread — a coarse whole-index try/except would blank healthy items too.
    by_thread: dict = {}
    by_spec: dict = {}
    by_task: dict = {}
    for w in items:
        try:
            for tid in w.thread_ids:
                by_thread[tid] = w.id
            if w.spec_id:
                by_spec[w.spec_id] = w.id
            for slug in w.task_slugs:
                by_task[slug] = w.id
        except Exception:
            continue
    return by_thread, by_spec, by_task


def _resolve_from_index(thread_id: str, spec_id: str | None, task_slug: str | None, index) -> str | None:
    """Resolve one thread against a prebuilt index. Precedence: explicit thread_ids link >
    spec_id > task_slug. Direct membership is exclusive, so this yields AT MOST ONE item."""
    by_thread, by_spec, by_task = index
    if thread_id in by_thread:
        return by_thread[thread_id]
    if spec_id and spec_id in by_spec:
        return by_spec[spec_id]
    if task_slug and task_slug in by_task:
        return by_task[task_slug]
    return None


def resolve_thread_work_item(
    thread_id: str,
    spec_id: str | None,
    task_slug: str | None,
    items: Iterable,
) -> str | None:
    """Return the id of the WorkItem related to a thread, or None.

    Precedence: explicit thread_ids link > spec_id > task_slug.
    `items` is any iterable of objects with .id/.spec_id/.task_slugs/.thread_ids.
    A thread resolves to AT MOST ONE WorkItem (direct membership is exclusive; the
    indirect fallback resolves to exactly one item deterministically).
    """
    return _resolve_from_index(thread_id, spec_id, task_slug, _build_link_index(items))


def index_thread_work_items(threads: Iterable, items: Iterable) -> dict[str, str | None]:
    """Best-effort batch of [resolve_thread_work_item] for a whole thread list: returns
    {thread.id: work_item_id or None}, building the reverse link index ONCE (not once per
    thread) for the GET /threads summary endpoint.

    Guarded so a single corrupt/unreadable WorkItem can never 500 the list — any failure
    falls back to None (the thread still lists, just without its work_item_id), mirroring
    get_spec's best-effort work_item_kind stamping. A thread never resolves to two items:
    direct thread_ids membership wins over the indirect spec_id/task_slug fallback.
    """
    try:
        index = _build_link_index(items)
    except Exception:
        index = ({}, {}, {})
    out: dict[str, str | None] = {}
    for t in threads:
        try:
            out[t.id] = _resolve_from_index(t.id, t.spec_id, t.task_slug, index)
        except Exception:
            out[t.id] = None
    return out
