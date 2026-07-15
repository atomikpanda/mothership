"""Read-time resolution of a thread's related WorkItem (inverts the WorkItem link graph)."""
from __future__ import annotations

from typing import Iterable


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
