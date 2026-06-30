from __future__ import annotations

import json
from pathlib import Path

from mship.core.claude_settings import (
    install_pretooluse_guard_hook, install_session_hook,
    GUARD_COMMAND, GUARD_MATCHER,
)


def _settings(ws: Path) -> dict:
    return json.loads((ws / ".claude" / "settings.json").read_text())


def test_installs_pretooluse_guard_into_fresh_settings(tmp_path: Path):
    assert install_pretooluse_guard_hook(tmp_path) == "installed"
    pre = _settings(tmp_path)["hooks"]["PreToolUse"]
    entry = pre[0]
    assert entry["matcher"] == GUARD_MATCHER
    assert entry["hooks"][0]["command"] == GUARD_COMMAND


def test_install_guard_is_idempotent(tmp_path: Path):
    assert install_pretooluse_guard_hook(tmp_path) == "installed"
    assert install_pretooluse_guard_hook(tmp_path) == "up to date"
    assert len(_settings(tmp_path)["hooks"]["PreToolUse"]) == 1


def test_guard_preserves_existing_session_hook(tmp_path: Path):
    install_session_hook(tmp_path)
    install_pretooluse_guard_hook(tmp_path)
    data = _settings(tmp_path)
    assert "SessionStart" in data["hooks"]
    assert "PreToolUse" in data["hooks"]


def test_guard_tolerates_malformed_settings(tmp_path: Path):
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text("{not json")
    assert "skipped" in install_pretooluse_guard_hook(tmp_path)
