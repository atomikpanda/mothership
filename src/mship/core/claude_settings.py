"""Install a Claude Code SessionStart hook into a workspace's .claude/settings.json
so each session surfaces the no-active-task notice. See spec enforcement-gate (MOS-189)."""
from __future__ import annotations

import json
from pathlib import Path

SESSION_COMMAND = "mship _session-context"


def install_session_hook(workspace_root: Path) -> str:
    """Idempotently add a SessionStart hook running `mship _session-context` to
    <workspace_root>/.claude/settings.json. Returns 'installed' or 'up to date'.
    Preserves all existing keys and hooks; tolerates a missing/malformed file."""
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
    session = hooks.setdefault("SessionStart", [])
    if not isinstance(session, list):
        session = hooks["SessionStart"] = []

    already = any(
        h.get("command") == SESSION_COMMAND
        for entry in session if isinstance(entry, dict)
        for h in (entry.get("hooks") or []) if isinstance(h, dict)
    )
    if already:
        return "up to date"

    session.append({"hooks": [{"type": "command", "command": SESSION_COMMAND}]})
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return "installed"
