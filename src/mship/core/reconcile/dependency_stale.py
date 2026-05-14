"""Post-process reconcile decisions to surface dependency_stale states (#104)."""
from __future__ import annotations

from dataclasses import replace as _replace
from datetime import datetime

from mship.core.reconcile.detect import UpstreamState


def apply_dependency_stale(state, decisions: dict) -> dict:
    """Override `in_sync` decisions to `dependency_stale` when any upstream
    has merged AFTER the downstream's task.created_at.

    Returns a new dict; does not mutate the input.
    """
    out = dict(decisions)
    for slug, task in state.tasks.items():
        d = out.get(slug)
        if d is None or d.state != UpstreamState.in_sync:
            continue
        for edge in task.depends_on:
            up = out.get(edge.upstream_slug)
            if up is None or up.state != UpstreamState.merged:
                continue
            up_merge_time = getattr(up, "merge_at", None) or _parse(getattr(up, "updated_at", None))
            if up_merge_time is None:
                continue
            if up_merge_time > task.created_at:
                out[slug] = _replace(d, state=UpstreamState.dependency_stale)
                break
    return out


def _parse(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
