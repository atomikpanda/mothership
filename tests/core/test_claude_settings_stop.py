# tests/core/test_claude_settings_stop.py
from __future__ import annotations

import json
from pathlib import Path

from mship.core.claude_settings import (
    install_stop_hook, install_session_hook, install_pretooluse_guard_hook,
    DRAIN_COMMAND,
)


def _settings(ws: Path) -> dict:
    return json.loads((ws / ".claude" / "settings.json").read_text())


def test_installs_stop_hook_into_fresh_settings(tmp_path: Path):
    assert install_stop_hook(tmp_path) == "installed"
    stop = _settings(tmp_path)["hooks"]["Stop"]
    assert stop[0]["hooks"][0]["command"] == DRAIN_COMMAND
    assert "matcher" not in stop[0]  # Stop hooks carry no matcher


def test_install_stop_is_idempotent(tmp_path: Path):
    assert install_stop_hook(tmp_path) == "installed"
    assert install_stop_hook(tmp_path) == "up to date"
    assert len(_settings(tmp_path)["hooks"]["Stop"]) == 1


def test_stop_preserves_other_hooks(tmp_path: Path):
    install_session_hook(tmp_path)
    install_pretooluse_guard_hook(tmp_path)
    install_stop_hook(tmp_path)
    hooks = _settings(tmp_path)["hooks"]
    assert "SessionStart" in hooks and "PreToolUse" in hooks and "Stop" in hooks


def test_stop_tolerates_malformed_settings(tmp_path: Path):
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text("{not json")
    assert "skipped" in install_stop_hook(tmp_path)
