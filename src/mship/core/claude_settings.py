"""Install Claude Code hooks into a workspace's .claude/settings.json.

Supports:
- SessionStart hook: surfaces the no-active-task notice each session
- PreToolUse guard hook: blocks edits to a repo's main checkout
- Stop hook: drains the message inbox at each turn boundary (`mship _drain`)

See spec enforcement-gate (MOS-189) and stop-hook-inbox-drain (#239).
"""
from __future__ import annotations

import json
from pathlib import Path

SESSION_COMMAND = "mship _session-context"
GUARD_COMMAND = "mship _guard-edit"
GUARD_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
DRAIN_COMMAND = "mship _drain"


def _install_hook_entry(workspace_root: Path, event_key: str, entry: dict, command: str) -> str:
    """Idempotently merge `entry` into hooks.<event_key> of
    <workspace_root>/.claude/settings.json, deduped by `command`. Preserves all
    existing keys/hooks; tolerates a missing/malformed file."""
    cdir = Path(workspace_root) / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    settings_path = cdir / "settings.json"

    data: dict = {}
    if settings_path.is_file():
        raw = settings_path.read_text()
        if raw.strip():
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError:
                return "skipped (settings.json is not valid JSON — fix it, then re-run)"
            if not isinstance(loaded, dict):
                return "skipped (settings.json is not a JSON object)"
            data = loaded

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = data["hooks"] = {}
    lst = hooks.setdefault(event_key, [])
    if not isinstance(lst, list):
        lst = hooks[event_key] = []

    already = any(
        h.get("command") == command
        for e in lst if isinstance(e, dict)
        for h in (e.get("hooks") or []) if isinstance(h, dict)
    )
    if already:
        return "up to date"

    lst.append(entry)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return "installed"


def install_session_hook(workspace_root: Path) -> str:
    """Idempotently add a SessionStart hook running `mship _session-context` to
    <workspace_root>/.claude/settings.json. Returns 'installed' or 'up to date'.
    Preserves all existing keys and hooks; tolerates a missing/malformed file."""
    return _install_hook_entry(
        workspace_root, "SessionStart",
        {"hooks": [{"type": "command", "command": SESSION_COMMAND}]},
        SESSION_COMMAND,
    )


def install_pretooluse_guard_hook(workspace_root: Path) -> str:
    """Idempotently add a PreToolUse guard hook running `mship _guard-edit`."""
    return _install_hook_entry(
        workspace_root, "PreToolUse",
        {"matcher": GUARD_MATCHER, "hooks": [{"type": "command", "command": GUARD_COMMAND}]},
        GUARD_COMMAND,
    )


def install_stop_hook(workspace_root: Path) -> str:
    """Idempotently add a Stop hook running `mship _drain` (drains the message
    inbox at each turn boundary). Stop hooks carry no matcher."""
    return _install_hook_entry(
        workspace_root, "Stop",
        {"hooks": [{"type": "command", "command": DRAIN_COMMAND}]},
        DRAIN_COMMAND,
    )
