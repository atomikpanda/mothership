import json as _json
import json
from pathlib import Path

import pytest

from mship.core.claude_settings import (
    SESSION_COMMAND,
    extract_claude_edit_targets,
    install_session_hook,
)


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


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            {"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}},
            ("src/a.py",),
        ),
        (
            {
                "tool_name": "NotebookEdit",
                "tool_input": {"notebook_path": "notebooks/a.ipynb"},
            },
            ("notebooks/a.ipynb",),
        ),
        (
            {
                "tool_name": "MultiEdit",
                "tool_input": {
                    "edits": [
                        {"file_path": "src/a.py"},
                        {"file_path": "src/b.py"},
                    ],
                },
            },
            ("src/a.py", "src/b.py"),
        ),
        (
            {
                "tool_name": "MultiEdit",
                "tool_input": {
                    "file_path": "src/a.py",
                    "edits": [
                        {"file_path": "src/a.py"},
                        {"file_path": "src/b.py"},
                    ],
                },
            },
            ("src/a.py", "src/b.py"),
        ),
        (
            {
                "tool_name": "MultiEdit",
                "tool_input": {
                    "edits": [
                        None,
                        {"file_path": ""},
                        {"file_path": 42},
                        {"file_path": "src/a.py"},
                        {"notebook_path": "notebooks/b.ipynb"},
                    ],
                },
            },
            ("src/a.py", "notebooks/b.ipynb"),
        ),
        (
            {"tool_name": "Bash", "tool_input": {"file_path": "src/a.py"}},
            (),
        ),
    ],
)
def test_extracts_documented_claude_edit_targets(event: dict, expected: tuple[str, ...]):
    assert extract_claude_edit_targets(event) == expected


def test_multi_edit_exposes_allowed_and_denied_targets(tmp_path: Path):
    worktree_target = tmp_path / ".worktrees" / "t" / "repo" / "src" / "a.py"
    main_target = tmp_path / "repo" / "src" / "b.py"

    assert extract_claude_edit_targets({
        "tool_name": "MultiEdit",
        "tool_input": {
            "edits": [
                {"file_path": str(worktree_target)},
                {"file_path": str(main_target)},
            ],
        },
    }) == (str(worktree_target), str(main_target))


@pytest.mark.parametrize(
    "event",
    [
        {"tool_name": "Edit", "tool_input": {}},
        {"tool_name": "Write", "tool_input": {"file_path": ""}},
        {"tool_name": "MultiEdit", "tool_input": {"edits": []}},
        {"tool_name": "NotebookEdit", "tool_input": None},
    ],
)
def test_recognized_claude_edit_without_targets_is_an_adapter_error(event: dict):
    with pytest.raises(ValueError, match="target"):
        extract_claude_edit_targets(event)
