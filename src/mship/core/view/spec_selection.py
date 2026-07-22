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


def scan_canonical_specs(specs_dir: Path) -> list[tuple[Spec, Path]]:
    """Parse every spec in the canonical `<workspace_root>/specs` store and return
    (spec, real_path) pairs sorted by (created_at, id). Suffix-aware via the storage
    layer: reads plaintext `.md` and decrypts `.md.enc` with the workspace key; a
    locked encrypted spec (no key) is skipped, as are files that are unreadable
    (OSError), invalid UTF-8 (UnicodeError), or unparseable (SpecParseError) so one
    bad file never aborts selection. Reads ONLY the workspace-root store, never a
    per-task worktree, so the result is branch/worktree independent (AC1).
    Returning the real path (not a reconstruction) keeps rendering correct even if
    a file's name diverges from `<date>-<id>.md`."""
    from mship.core.spec_storage import SpecStorage
    if not specs_dir.is_dir():
        return []
    storage = SpecStorage(specs_dir)  # workspace_root defaults to specs_dir.parent
    out: list[tuple[Spec, Path]] = []
    for spec, _locked_id, path in storage.read_all():
        if spec is None:
            continue  # locked: no key, skip
        out.append((spec, path))
    return sorted(out, key=lambda sp: _sort_key(sp[0]))


def load_canonical_specs(specs_dir: Path) -> list[Spec]:
    """The specs from `scan_canonical_specs`, without their paths (for selection)."""
    return [spec for spec, _ in scan_canonical_specs(specs_dir)]


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
