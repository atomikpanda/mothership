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
            work_item_id, ownership_ambiguous = _resolve_with_ambiguity(
                thread.id, thread.spec_id, thread.task_slug, index,
            )
            item = items_by_id.get(work_item_id)
            ambiguous_spec = item is not None and item.spec_id in ambiguous_spec_ids
            terminal = not ownership_ambiguous and not ambiguous_spec and item is not None and compute_phase(
                item,
                specs_by_id.get(item.spec_id) if item.spec_id else None,
                [tasks_by_slug[slug] for slug in item.task_slugs if slug in tasks_by_slug],
            ) == "done"
            links[thread.id] = ThreadInboxLink(
                work_item_id,
                terminal,
                ownership_ambiguous
                or ambiguous_spec
                or (uncertain and work_item_id is None)
                or (work_item_id is None and bool(thread.spec_id or thread.task_slug)),
            )
        except Exception:
            links[thread.id] = ThreadInboxLink(None, False, True)
    return links

def _build_link_index(items: Iterable):
    """Build reverse owner sets for thread, spec, and task links once per batch."""
    by_thread: dict[str, set[str]] = {}
    by_spec: dict[str, set[str]] = {}
    by_task: dict[str, set[str]] = {}
    for w in items:
        try:
            for tid in w.thread_ids:
                by_thread.setdefault(tid, set()).add(w.id)
            if w.spec_id:
                by_spec.setdefault(w.spec_id, set()).add(w.id)
            for slug in w.task_slugs:
                by_task.setdefault(slug, set()).add(w.id)
        except Exception:
            continue
    return by_thread, by_spec, by_task


def _resolve_with_ambiguity(
    thread_id: str, spec_id: str | None, task_slug: str | None, index,
) -> tuple[str | None, bool]:
    """Resolve one link at the first applicable precedence, preserving ambiguity."""
    by_thread, by_spec, by_task = index
    for key, owners in (
        (thread_id, by_thread),
        (spec_id, by_spec),
        (task_slug, by_task),
    ):
        if key and key in owners:
            matches = owners[key]
            return (next(iter(matches)), False) if len(matches) == 1 else (None, True)
    return None, False


def _resolve_from_index(thread_id: str, spec_id: str | None, task_slug: str | None, index) -> str | None:
    return _resolve_with_ambiguity(thread_id, spec_id, task_slug, index)[0]


def resolve_thread_work_item(
    thread_id: str,
    spec_id: str | None,
    task_slug: str | None,
    items: Iterable,
) -> str | None:
    """Return the id of the WorkItem related to a thread, or None.

    Precedence: explicit thread_ids link > spec_id > task_slug.
    `items` is any iterable of objects with .id/.spec_id/.task_slugs/.thread_ids.
    A duplicate owner at the selected precedence is ambiguous and resolves to None.
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
