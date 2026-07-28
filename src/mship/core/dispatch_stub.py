"""Controller-facing dispatch stub (spec mship-dispatch-v2, ac3).

A CLOSED set of fields. The controller's context carries orchestration facts
only; every byte of prompt content (plan slice, template, acceptance text)
reaches the subagent alone via `mship dispatch --emit`. Adding a field here
means updating the closed-set test in tests/core/test_dispatch_stub.py — that
friction is the point.
"""
from __future__ import annotations

from mship.core.sdd_store import DispatchRecord

STUB_FIELDS = ("record", "model", "mode", "worktree", "emit")


def build_stub(rec: DispatchRecord, *, record_path: str) -> str:
    return (
        f"record: {record_path}\n"
        f"model: {rec.model}\n"
        f"mode: {rec.mode}\n"
        f"worktree: {rec.worktree}\n"
        f"emit: run subagent with cwd={rec.worktree}, model={rec.model}; "
        f"its first command: `mship dispatch --emit`\n"
    )
