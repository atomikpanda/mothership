"""Spec lifecycle helpers for automated status transitions."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def advance_spec_on_close(
    *,
    task,
    specs_dir: Path,
    merged_count: int,
    closed_count: int,
) -> None:
    """Advance a bound spec from dispatched → implemented when all PRs merged.

    Safe no-op if:
    - task.spec_id is None
    - spec file doesn't exist
    - spec is not in dispatched state
    - not all PRs merged (merged_count == 0 or closed_count > 0)
    """
    if not getattr(task, "spec_id", None):
        return
    if merged_count == 0 or closed_count > 0:
        return

    from mship.core.spec_store import SpecStore

    store = SpecStore(specs_dir)
    spec = store.find_by_id(task.spec_id)
    if spec is None or spec.status != "dispatched":
        return

    spec.status = "implemented"
    spec.updated_at = datetime.now(timezone.utc)
    store.save(spec)
