"""Shared `--follow` resolution for the view commands (spec cockpit-v2, ac2).

`--follow` points a view at the workspace CURRENT FOCUS (written by
`mship layout focus`) instead of a fixed --task/--workitem, re-resolving at render
time so a focus change re-scopes the pane. This module owns the two shared pieces:
reading the focused id from the focus file, and the single empty-state hint string
every follow view shows when nothing is focused.
"""
from __future__ import annotations

from mship.core.focus import focus_path, read_focus


def read_focused_id(container) -> str | None:
    """The currently-focused WorkItem id for this workspace, or None when nothing
    is focused (or the focus file is missing/corrupt)."""
    state = read_focus(focus_path(container.state_dir()))
    return state.work_item_id if state is not None else None


def follow_hint() -> str:
    """The clear empty state shown by a `--follow` view when no WorkItem is focused
    (ac2) — never an error."""
    return ("No WorkItem focused. Press enter on an item in the Overview tab, "
            "or run `mship layout focus <id>`.")
