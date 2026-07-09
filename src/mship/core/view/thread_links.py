"""Read-time resolution of a thread's related WorkItem (inverts the WorkItem link graph)."""
from __future__ import annotations

from typing import Iterable


def resolve_thread_work_item(
    thread_id: str,
    spec_id: str | None,
    task_slug: str | None,
    items: Iterable,
) -> str | None:
    """Return the id of the WorkItem related to a thread, or None.

    Precedence: explicit thread_ids link > spec_id > task_slug.
    `items` is any iterable of objects with .id/.spec_id/.task_slugs/.thread_ids.
    """
    items = list(items)
    by_thread = {tid: w.id for w in items for tid in w.thread_ids}
    if thread_id in by_thread:
        return by_thread[thread_id]
    if spec_id:
        by_spec = {w.spec_id: w.id for w in items if w.spec_id}
        if spec_id in by_spec:
            return by_spec[spec_id]
    if task_slug:
        by_task = {slug: w.id for w in items for slug in w.task_slugs}
        if task_slug in by_task:
            return by_task[task_slug]
    return None
