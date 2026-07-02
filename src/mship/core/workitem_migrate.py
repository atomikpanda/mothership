from __future__ import annotations

from datetime import datetime

from mship.core.message_store import MessageStore
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager
from mship.core.workitem_store import WorkItemStore

# "approved or beyond" — matches workitem_gate._APPROVED / PhaseManager._has_approved_spec.
_APPROVED = {"approved", "dispatched", "implemented"}


def wrap_existing(items: WorkItemStore, specs: SpecStore, state: StateManager,
                  msgs: MessageStore, now: datetime, workspace: str) -> list[str]:
    """Idempotently wrap every spec/task lacking a work_item_id in a WorkItem.
    Returns the ids of newly created items."""
    created: list[str] = []

    state_now = state.load()
    task_by_slug = dict(state_now.tasks)

    # 1) Specs -> feature items (carrying their linked task).
    for spec in specs.list():
        if spec.work_item_id:
            continue
        wi = items.create(title=spec.title, kind="feature", workspace=workspace, now=now)
        created.append(wi.id)
        items.link_spec(wi.id, spec.id, now=now)
        spec.work_item_id = wi.id
        specs.save(spec)
        if spec.task_slug and spec.task_slug in task_by_slug:
            items.add_task(wi.id, spec.task_slug, now=now)

            def _set(s, _slug=spec.task_slug, _wid=wi.id):
                if _slug in s.tasks:
                    s.tasks[_slug].work_item_id = _wid
            state.mutate(_set)

    # 2) Orphan tasks (no work_item_id yet) -> feature or chore items. A task
    # whose spec_id (or a reverse task_slug match) resolves to an
    # approved-or-beyond spec gets its own feature item linked to that spec;
    # otherwise it gets a plain chore item, same as a task with no spec at
    # all. Tasks pass 1 already linked (forward spec.task_slug match) already
    # carry work_item_id by the time state is reloaded below, so they're
    # skipped here — no double-creation.
    all_specs = specs.list()
    spec_by_id = {s.id: s for s in all_specs}
    spec_by_task_slug = {s.task_slug: s for s in all_specs if s.task_slug}
    for slug, task in state.load().tasks.items():
        if task.work_item_id:
            continue
        spec = (spec_by_id.get(task.spec_id) if task.spec_id else None) \
            or spec_by_task_slug.get(slug)
        if spec is not None and spec.status in _APPROVED:
            wi = items.create(title=task.description or slug, kind="feature",
                              workspace=workspace, now=now)
            created.append(wi.id)
            items.link_spec(wi.id, spec.id, now=now)
        else:
            wi = items.create(title=task.description or slug, kind="chore",
                              workspace=workspace, now=now)
            created.append(wi.id)
        items.add_task(wi.id, slug, now=now)

        def _set(s, _slug=slug, _wid=wi.id):
            if _slug in s.tasks:
                s.tasks[_slug].work_item_id = _wid
        state.mutate(_set)

    # 3) Threads -> attach to the item their spec/task already belongs to.
    all_items = items.list()
    item_by_spec = {w.spec_id: w.id for w in all_items if w.spec_id}
    item_by_task = {slug: w.id for w in all_items for slug in w.task_slugs}
    for thread in msgs.list():
        target = (item_by_spec.get(thread.spec_id) if thread.spec_id else None) \
            or (item_by_task.get(thread.task_slug) if thread.task_slug else None)
        if target:
            items.add_thread(target, thread.id, now=now)

    return created
