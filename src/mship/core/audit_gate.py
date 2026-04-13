"""Shared audit-gate logic used by mship spawn and mship finish."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from mship.core.repo_state import AuditReport


class AuditGateBlocked(RuntimeError):
    """Raised when the audit gate blocks the command."""


def run_audit_gate(
    report: AuditReport,
    *,
    block: bool,
    force: bool,
    command_name: str,
    on_bypass: Callable[[list[str]], None],
) -> None:
    """Apply the audit gate.

    - No errors: return silently.
    - Errors + force=True: call on_bypass(codes) and return.
    - Errors + block=True: raise AuditGateBlocked with the issue summary.
    - Errors + block=False: print nothing here; caller is expected to warn.
    """
    if not report.has_errors:
        return

    error_codes: list[str] = []
    for repo in report.repos:
        for issue in repo.issues:
            if issue.severity == "error":
                error_codes.append(f"{repo.name}:{issue.code}")

    if force:
        on_bypass(error_codes)
        return

    if block:
        raise AuditGateBlocked(
            f"{command_name} blocked by audit — "
            + ", ".join(error_codes)
        )
    # block=False, not forced: caller handles warning


def collect_known_worktree_paths(state_manager) -> "frozenset[Path]":
    """Return a resolved, absolute set of every worktree path in every task."""
    state = state_manager.load()
    paths: set[Path] = set()
    for task in state.tasks.values():
        for raw in task.worktrees.values():
            paths.add(Path(raw).resolve())
    return frozenset(paths)
