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
    completed_without_prs: bool = False,
) -> None:
    """Advance a bound spec from dispatched → implemented when the close delivered
    the work: all PRs merged, or a PR-less completion the caller vouches for.

    `completed_without_prs` is the honest shape for the `finish --push-only` →
    local merge → `close` route: no PRs ever exist there, so merged_count can
    never say "delivered" — the caller (cli close) asserts completion explicitly
    instead of faking a merge count. It must only be passed for a finished,
    pushed, non-abandoned, non-forced close.

    Safe no-op if:
    - task.spec_id is None
    - spec file doesn't exist
    - spec is not in dispatched state
    - not delivered (no clean full merge AND not completed_without_prs)
    """
    if not getattr(task, "spec_id", None):
        return
    if not completed_without_prs and (merged_count == 0 or closed_count > 0):
        return

    from mship.core.spec_store import SpecStore

    store = SpecStore(specs_dir)
    spec = store.read_strict(task.spec_id)
    if spec is None or spec.status != "dispatched":
        return

    spec.status = "implemented"
    spec.updated_at = datetime.now(timezone.utc)
    store.save(spec)
