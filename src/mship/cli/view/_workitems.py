"""Shared view-CLI helper: build the WorkItem summary index from the workspace-
canonical stores. Reused by status/journal/diff/spec so each command wires the
same phase-aware index (headers, grouping) without duplicating store wiring."""
from __future__ import annotations

from pathlib import Path

from mship.core.message_store import MessageStore
from mship.core.spec_store import SPECS_DIRNAME, SpecStore
from mship.core.view.workitem_index import WorkItemSummary, build_workitem_index
from mship.core.workitem_store import WorkItemStore


def load_workitem_index(container) -> list[WorkItemSummary]:
    """Build the WorkItem index (derived phase + attention + task_slugs + spec_id)
    from the canonical stores under the workspace root and state dir. Best-effort:
    a failure loading the core stores (workitems / specs / tasks) degrades to an
    empty index so a view never crashes."""
    try:
        state_dir = Path(container.state_dir())
        workspace_root = Path(container.config_path()).parent
        items = WorkItemStore(state_dir / "workitems").list()
        specs = {s.id: s for s in SpecStore(workspace_root / SPECS_DIRNAME).list()}
        tasks = dict(container.state_manager().load().tasks)
    except Exception:
        return []
    # Threads only add per-item thread links; a broken message store must NOT
    # erase the WorkItem grouping/headers already built from items/specs/tasks.
    try:
        threads = {t.id: t for t in MessageStore(state_dir / "messages").list()}
    except Exception:
        threads = {}
    try:
        return build_workitem_index(items, specs, tasks, threads)
    except Exception:
        return []
