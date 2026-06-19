import json as _json
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


def test_install_skips_malformed_json_without_destroying_it(tmp_path):
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text("{ not json")
    outcome = install_session_hook(tmp_path)
    assert outcome.startswith("skipped")
    # original content is preserved, not overwritten
    assert (cdir / "settings.json").read_text() == "{ not json"


def test_install_into_empty_file(tmp_path):
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text("")
    assert install_session_hook(tmp_path) == "installed"
    cmds = [h["command"] for e in _hooks(tmp_path) for h in e["hooks"]]
    assert SESSION_COMMAND in cmds


def test_install_tolerates_null_hooks_entry(tmp_path):
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text(_json.dumps(
        {"hooks": {"SessionStart": [{"hooks": None}]}}
    ))
    # must not raise; installs the hook
    assert install_session_hook(tmp_path) == "installed"
    cmds = [h["command"] for e in _hooks(tmp_path) for h in (e.get("hooks") or []) if isinstance(h, dict)]
    assert SESSION_COMMAND in cmds
