"""Deterministic enforcement-gate helpers: bypass resolution + logging, and the
session-start no-active-task notice. See spec enforcement-gate (MOS-189)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

NO_TASK_NOTICE = (
    "mothership workspace with no active task. Run `mship spawn \"<description>\"` "
    "before editing source files — commits/pushes of untasked feature work are gated."
)

_BYPASS_ENV = "MSHIP_BYPASS_GATE"


def resolve_bypass() -> tuple[bool, str]:
    """(bypassed, reason) from MSHIP_BYPASS_GATE. Unset/blank or an explicit
    off-value (0/false/no, case-insensitive) -> (False, ""). A bare "1" ->
    (True, ""); any other value -> (True, <that value as the reason>)."""
    val = os.environ.get(_BYPASS_ENV)
    if not val or not val.strip():
        return (False, "")
    reason = val.strip()
    if reason.lower() in ("0", "false", "no"):
        return (False, "")
    return (True, "" if reason == "1" else reason)


def record_bypass(workspace_root: Path, *, op: str, branch: str, reason: str) -> None:
    """Append a bypass record to <workspace_root>/.mothership/bypass-log.jsonl."""
    sd = Path(workspace_root) / ".mothership"
    sd.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "op": op,
        "branch": branch,
        "reason": reason,
        "cwd": str(Path.cwd()),
    }
    with (sd / "bypass-log.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


def no_task_notice(cwd: Path) -> str | None:
    """Return the notice when `cwd` is in a mship workspace with no active task,
    else None. Fail-open (None) on any error — this is advisory context."""
    try:
        from mship.core.config import ConfigLoader
        from mship.core.state import StateManager
        try:
            config_path = ConfigLoader.discover(cwd)
        except FileNotFoundError:
            return None
        state = StateManager(config_path.parent / ".mothership").load()
        active = [t for t in state.tasks.values() if t.finished_at is None]
        return None if active else NO_TASK_NOTICE
    except Exception:
        return None


def messaging_notice(cwd: Path) -> str | None:
    """A one-line nudge for the SessionStart hook: keep a background
    `mship inbox wait` armed so this session wakes on a new phone message.
    Returns None outside a workspace, or when the mailbox has no threads yet
    (quiet until messaging is actually in use). Fail-open (None) on any error."""
    try:
        from mship.core.config import ConfigLoader
        from mship.core.message_store import MessageStore
        try:
            config_path = ConfigLoader.discover(cwd)
        except FileNotFoundError:
            return None
        ws_root = Path(config_path).parent
        if not MessageStore(ws_root / ".mothership" / "messages").list():
            return None
    except Exception:
        return None
    return (
        "Phone messages may arrive mid-session. To answer them while idle, keep a "
        "background `mship inbox wait` armed and re-arm after each reply — see the "
        "`receiving-messages` skill. (Messages mid-turn are also caught by the Stop hook.)"
    )
