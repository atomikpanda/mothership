"""Debug-thread derivation from the per-task journal.

"Open thread" is computed purely from journal content — no state-file
mutation. The thread is the sequence of entries from the FIRST `action=hypothesis`
after the most recent `action=debug-resolved` (or from task start if never
resolved), continuing to the end of the journal. Returns None when there is no
open thread. See #30.
"""
from __future__ import annotations

from mship.core.log import LogEntry, LogManager


def current_debug_thread(log: LogManager, slug: str) -> list[LogEntry] | None:
    """Return the list of entries in the current open debug thread, or None.

    The thread opens at the first `hypothesis` action after the most recent
    `debug-resolved` (or from task start if there has been no resolution).
    It remains open until the end of the journal or the next `debug-resolved`.
    """
    entries = log.read(slug)
    if not entries:
        return None

    # Find the index of the most recent `debug-resolved`. Everything before
    # or at that index is closed.
    last_resolved_idx = -1
    for i, e in enumerate(entries):
        if e.action == "debug-resolved":
            last_resolved_idx = i

    # Search for the first `hypothesis` entry AFTER that boundary.
    first_hypothesis_idx = None
    for i in range(last_resolved_idx + 1, len(entries)):
        if entries[i].action == "hypothesis":
            first_hypothesis_idx = i
            break
    if first_hypothesis_idx is None:
        return None

    return entries[first_hypothesis_idx:]
