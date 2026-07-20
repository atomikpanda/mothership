"""Canonical spec resolution + selection for `mship view spec` (AC1, AC2).

Pure over already-parsed `Spec`/`WorkItem` objects, plus one resilient reader of
the workspace-canonical `<workspace_root>/specs` store. Deterministic: selection
is by created_at + id, never filesystem mtime.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mship.core.spec import Spec
from mship.core.workitem import WorkItem


class SpecSelectionError(Exception):
    """No spec matched the requested selector (work item / status / default)."""


def _sort_key(spec: Spec) -> tuple:
    # Deterministic newest-first ordering: created_at, then id. NOT mtime.
    return (spec.created_at, spec.id)


def load_canonical_specs(specs_dir: Path) -> list[Spec]:
    """Parse every spec in the canonical `<workspace_root>/specs` store, skipping
    unparseable files. Reads ONLY the workspace-root store, never a per-task
    worktree, so the result is branch/worktree independent (AC1). Sorted by
    (created_at, id) ascending."""
    from mship.core.spec_store import SpecParseError, parse_spec
    if not specs_dir.is_dir():
        return []
    out: list[Spec] = []
    for p in sorted(specs_dir.glob("*.md")):
        try:
            out.append(parse_spec(p.read_text()))
        except SpecParseError:
            continue
    return sorted(out, key=_sort_key)


@dataclass(frozen=True)
class SpecSelector:
    work_item_id: str | None = None
    status: str | None = None


def select_default(specs: list[Spec]) -> Spec:
    """Documented default when `mship view spec` gets no selector: the most
    recently CREATED non-archived spec (created_at, then id), from the canonical
    store. Deterministic — never filesystem mtime (AC2)."""
    pool = [s for s in specs if s.status != "archived"] or list(specs)
    if not pool:
        raise SpecSelectionError("No specs in the canonical store.")
    return max(pool, key=_sort_key)


def select_by_status(specs: list[Spec], status: str) -> Spec:
    matches = [s for s in specs if s.status == status]
    if not matches:
        raise SpecSelectionError(f"No spec with status {status!r} in the canonical store.")
    return max(matches, key=_sort_key)


def select_by_workitem(specs: list[Spec], workitems: list[WorkItem], work_item_id: str) -> Spec:
    item = next((w for w in workitems if w.id == work_item_id), None)
    if item is None:
        raise SpecSelectionError(f"Unknown work item: {work_item_id!r}")
    if item.spec_id is None:
        raise SpecSelectionError(f"Work item {work_item_id!r} has no linked spec.")
    spec = next((s for s in specs if s.id == item.spec_id), None)
    if spec is None:
        raise SpecSelectionError(
            f"Work item {work_item_id!r} links spec {item.spec_id!r}, absent from the store."
        )
    return spec


def select_spec(specs: list[Spec], workitems: list[WorkItem], selector: SpecSelector) -> Spec:
    """Resolve one spec per `selector` (AC2). Precedence: work item > status >
    deterministic default. Raises SpecSelectionError on no match."""
    if selector.work_item_id is not None:
        return select_by_workitem(specs, workitems, selector.work_item_id)
    if selector.status is not None:
        return select_by_status(specs, selector.status)
    return select_default(specs)
