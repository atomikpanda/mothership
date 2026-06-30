from __future__ import annotations

from pathlib import Path

from mship.core.gate import messaging_notice


def test_messaging_notice_in_workspace(tmp_path: Path):
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    notice = messaging_notice(tmp_path)
    assert notice is not None
    assert "inbox wait" in notice
    assert "receiving-messages" in notice


def test_messaging_notice_outside_workspace_is_none(tmp_path: Path):
    assert messaging_notice(tmp_path) is None
