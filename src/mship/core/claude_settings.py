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
from typing import Any

SESSION_COMMAND = "mship _session-context"
GUARD_COMMAND = "mship _guard-edit"
GUARD_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
DRAIN_COMMAND = "mship _drain"

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def extract_claude_edit_targets(event: dict[str, Any]) -> tuple[str, ...]:
    """Extract every path represented by Claude Edit/Write/MultiEdit/NotebookEdit."""
    if event.get("tool_name") not in _EDIT_TOOLS:
        return ()
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError("guarded edit did not contain a target path")

    targets: list[str] = []
    for key in ("file_path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            targets.append(value)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            value = edit.get("file_path") or edit.get("notebook_path")
            if isinstance(value, str) and value:
                targets.append(value)
    if not targets:
        raise ValueError("guarded edit did not contain a target path")
    return tuple(dict.fromkeys(targets))


CLAUDE_ENTRIES = {
    "SessionStart": {
        "hooks": [{"type": "command", "command": SESSION_COMMAND}],
    },
    "PreToolUse": {
        "matcher": GUARD_MATCHER,
        "hooks": [{"type": "command", "command": GUARD_COMMAND}],
    },
    "Stop": {
        "hooks": [{"type": "command", "command": DRAIN_COMMAND}],
    },
}


def _is_owned_handler(handler: object, command: str) -> bool:
    return isinstance(handler, dict) and handler.get("command") == command


def _owned_handler_count(groups: list, command: str) -> int:
    count = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            continue
        count += sum(
            1 for handler in handlers if _is_owned_handler(handler, command)
        )
    return count


def registration_issues(data: dict) -> list[str]:
    """Return Claude events whose Mothership registration is missing or stale."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return list(CLAUDE_ENTRIES)
    issues: list[str] = []
    for event, desired in CLAUDE_ENTRIES.items():
        groups = hooks.get(event)
        if not isinstance(groups, list):
            issues.append(event)
            continue
        command = desired["hooks"][0]["command"]
        owned_count = _owned_handler_count(groups, command)
        if owned_count != 1 or not any(group == desired for group in groups):
            issues.append(event)
    return issues




def _install_hook_entry(workspace_root: Path, event_key: str, entry: dict, command: str) -> str:
    """Reconcile one Mothership-owned Claude hook while preserving foreign config."""
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
    groups = hooks.setdefault(event_key, [])
    if not isinstance(groups, list):
        groups = hooks[event_key] = []

    owned_count = _owned_handler_count(groups, command)
    if owned_count == 1 and any(group == entry for group in groups):
        return "up to date"

    reconciled: list = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            reconciled.append(group)
            continue
        handlers = group["hooks"]
        retained = [
            handler for handler in handlers
            if not _is_owned_handler(handler, command)
        ]
        if retained:
            updated = dict(group)
            updated["hooks"] = retained
            reconciled.append(updated)
        elif not any(_is_owned_handler(handler, command) for handler in handlers):
            reconciled.append(group)
        elif set(group) - {"matcher", "hooks"}:
            updated = dict(group)
            updated["hooks"] = []
            reconciled.append(updated)

    reconciled.append(entry)
    hooks[event_key] = reconciled
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return "updated" if owned_count else "installed"


def install_session_hook(workspace_root: Path) -> str:
    """Idempotently add a SessionStart hook running `mship _session-context` to
    <workspace_root>/.claude/settings.json. Returns 'installed' or 'up to date'.
    Preserves all existing keys and hooks; tolerates a missing/malformed file."""
    return _install_hook_entry(
        workspace_root, "SessionStart", CLAUDE_ENTRIES["SessionStart"], SESSION_COMMAND
    )


def install_pretooluse_guard_hook(workspace_root: Path) -> str:
    """Idempotently add a PreToolUse guard hook running `mship _guard-edit`."""
    return _install_hook_entry(
        workspace_root, "PreToolUse", CLAUDE_ENTRIES["PreToolUse"], GUARD_COMMAND
    )


def install_stop_hook(workspace_root: Path) -> str:
    """Idempotently add a Stop hook running `mship _drain` (drains the message
    inbox at each turn boundary). Stop hooks carry no matcher."""
    return _install_hook_entry(
        workspace_root, "Stop", CLAUDE_ENTRIES["Stop"], DRAIN_COMMAND
    )
