"""Map task-resolver exceptions to short placeholder strings used by views
in --watch mode when a task cannot be resolved yet.

Centralising the strings here lets tests assert against the same source of
wording the views render, avoiding copy drift between implementation and
tests.
"""
from __future__ import annotations

from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
)


def placeholder_for(err: Exception) -> str:
    if isinstance(err, NoActiveTaskError):
        return 'No active task. Run `mship spawn "description"` to start one.'
    if isinstance(err, AmbiguousTaskError):
        return (
            f"Multiple active tasks ({', '.join(err.active)}). "
            "Pass --task, set MSHIP_TASK, or close extras."
        )
    if isinstance(err, UnknownTaskError):
        return f"Task '{err.slug}' not found. Waiting for it to be spawned."
    raise err
