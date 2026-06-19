import json
from pathlib import Path

from mship.core.claude_settings import install_session_hook, SESSION_COMMAND


def _hooks(ws: Path):
    return json.loads((ws / ".claude" / "settings.json").read_text())["hooks"]["SessionStart"]


def test_install_creates_settings(tmp_path):
    outcome = install_session_hook(tmp_path)
    assert outcome == "installed"
    cmds = [h["command"] for e in _hooks(tmp_path) for h in e["hooks"]]
    assert SESSION_COMMAND in cmds


def test_install_is_idempotent(tmp_path):
    install_session_hook(tmp_path)
    outcome2 = install_session_hook(tmp_path)
    assert outcome2 == "up to date"
    cmds = [h["command"] for e in _hooks(tmp_path) for h in e["hooks"]]
    assert cmds.count(SESSION_COMMAND) == 1


def test_install_preserves_existing(tmp_path):
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        "model": "sonnet",
    }))
    install_session_hook(tmp_path)
    data = json.loads((cdir / "settings.json").read_text())
    assert data["model"] == "sonnet"
    cmds = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert "echo hi" in cmds and SESSION_COMMAND in cmds


def test_install_handles_malformed_json(tmp_path):
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text("{ not json")
    # Should not crash; treats as empty and installs.
    install_session_hook(tmp_path)
    cmds = [h["command"] for e in _hooks(tmp_path) for h in e["hooks"]]
    assert SESSION_COMMAND in cmds
