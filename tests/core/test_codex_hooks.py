from __future__ import annotations

import json
from pathlib import Path

import pytest

from mship.core.codex_hooks import (
    CODEX_COMMANDS,
    extract_edit_targets,
    install_codex_hooks,
)


def _event(tool_name: str, tool_input: dict) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        (
            "*** Begin Patch\n*** Update File: src/a.py\n*** End Patch\n",
            ("src/a.py",),
        ),
        (
            "*** Begin Patch\n*** Add File: src/new.py\n*** Delete File: src/old.py\n*** End Patch\n",
            ("src/new.py", "src/old.py"),
        ),
        (
            "*** Begin Patch\n*** Update File: src/old.py\n*** Move to: src/new.py\n*** End Patch\n",
            ("src/old.py", "src/new.py"),
        ),
        (
            "*** Begin Patch\n*** Update File: \"src/a file.py\"\n*** Add File: 'src/b file.py'\n*** End Patch\n",
            ("src/a file.py", "src/b file.py"),
        ),
        (
            "*** Begin Patch\n*** Update File: /tmp/absolute.py\n*** Update File: /tmp/absolute.py\n*** End Patch\n",
            ("/tmp/absolute.py",),
        ),
    ],
)
def test_extracts_every_apply_patch_target(patch: str, expected: tuple[str, ...]):
    assert extract_edit_targets(_event("apply_patch", {"command": patch})) == expected


@pytest.mark.parametrize("alias", ["Edit", "Write", "NotebookEdit"])
def test_extracts_documented_edit_alias_path(alias: str):
    event = _event(alias, {"file_path": "src/a.py"})
    assert extract_edit_targets(event) == ("src/a.py",)


def test_extracts_multi_edit_targets():
    event = _event(
        "MultiEdit",
        {
            "edits": [
                {"file_path": "src/a.py"},
                {"file_path": "src/b.py"},
                {"file_path": "src/a.py"},
            ]
        },
    )
    assert extract_edit_targets(event) == ("src/a.py", "src/b.py")


@pytest.mark.parametrize(
    "event",
    [
        _event("apply_patch", {"command": "*** Begin Patch\n*** End Patch\n"}),
        _event("apply_patch", {"command": "not a patch"}),
        _event("Write", {}),
    ],
)
def test_recognized_edit_without_targets_is_an_adapter_error(event: dict):
    with pytest.raises(ValueError, match="target"):
        extract_edit_targets(event)


def test_unrelated_tool_has_no_guard_targets():
    assert extract_edit_targets(_event("Bash", {"command": "echo ok"})) == ()


def test_install_creates_all_codex_event_bindings(tmp_path: Path):
    result = install_codex_hooks(tmp_path)

    data = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert result.status == "installed"
    assert set(data["hooks"]) == {"SessionStart", "PreToolUse", "Stop"}
    commands = {
        event: [
            hook["command"]
            for group in data["hooks"][event]
            for hook in group["hooks"]
        ]
        for event in data["hooks"]
    }
    assert commands == {event: [command] for event, command in CODEX_COMMANDS.items()}


def test_install_is_byte_idempotent(tmp_path: Path):
    install_codex_hooks(tmp_path)
    path = tmp_path / ".codex" / "hooks.json"
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    result = install_codex_hooks(tmp_path)

    assert result.status == "up to date"
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_install_preserves_user_configuration_and_entries(tmp_path: Path):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "description": "user hooks",
        "custom": {"keep": True},
        "hooks": {
            "SessionStart": [{"matcher": "resume", "hooks": [{"type": "command", "command": "echo user"}]}],
            "PostToolUse": [{"hooks": [{"type": "command", "command": "echo post"}]}],
        },
    }))

    result = install_codex_hooks(tmp_path)
    data = json.loads(path.read_text())

    assert result.status == "installed"
    assert data["description"] == "user hooks"
    assert data["custom"] == {"keep": True}
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo user"
    assert data["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "echo post"


def test_install_replaces_only_stale_owned_handlers(tmp_path: Path):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{
                "matcher": "Write",
                "hooks": [
                    {"type": "command", "command": "echo user"},
                    {"type": "command", "command": "mship _guard-edit --runtime codex --legacy", "timeout": 1},
                    {"type": "command", "command": "mship _guard-edit --runtime codex"},
                ],
            }],
        }
    }))

    result = install_codex_hooks(tmp_path)
    data = json.loads(path.read_text())
    pretool_commands = [
        hook["command"]
        for group in data["hooks"]["PreToolUse"]
        for hook in group["hooks"]
    ]

    assert result.status == "updated"
    assert pretool_commands.count("echo user") == 1
    assert pretool_commands.count(CODEX_COMMANDS["PreToolUse"]) == 1
    assert all("--legacy" not in command for command in pretool_commands)


def test_malformed_json_is_preserved(tmp_path: Path):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    malformed = '{"hooks": '
    path.write_text(malformed)

    result = install_codex_hooks(tmp_path)

    assert result.status == "skipped"
    assert "valid JSON" in result.message
    assert path.read_text() == malformed


@pytest.mark.parametrize("malformed", ["", " \n\t"])
def test_empty_or_whitespace_json_is_preserved(tmp_path: Path, malformed: str):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(malformed)

    result = install_codex_hooks(tmp_path)

    assert result.status == "skipped"
    assert "valid JSON" in result.message
    assert path.read_text() == malformed

@pytest.mark.parametrize(
    "data",
    [
        {"hooks": []},
        {"hooks": {"Stop": {}}},
        {"hooks": {"Stop": [{"hooks": 7}]}},
    ],
)
def test_unsafe_partial_shape_is_preserved(tmp_path: Path, data: dict):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    original = json.dumps(data)
    path.write_text(original)

    result = install_codex_hooks(tmp_path)

    assert result.status == "skipped"
    assert path.read_text() == original


def test_atomic_write_failure_preserves_existing_file(tmp_path: Path, monkeypatch):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    original = json.dumps({"custom": True})
    path.write_text(original)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("mship.core.codex_hooks._atomic_write", fail)

    with pytest.raises(OSError, match="disk full"):
        install_codex_hooks(tmp_path)
    assert path.read_text() == original
