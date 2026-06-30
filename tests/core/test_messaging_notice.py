from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mship.core.gate import messaging_notice
from mship.core.message_store import MessageStore


def test_messaging_notice_in_workspace(tmp_path: Path):
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    MessageStore(tmp_path / ".mothership" / "messages").create_thread(
        "s", "hi", datetime.now(timezone.utc)
    )
    notice = messaging_notice(tmp_path)
    assert notice is not None
    assert "inbox wait" in notice
    assert "receiving-messages" in notice


def test_messaging_notice_none_when_mailbox_empty(tmp_path: Path):
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    assert messaging_notice(tmp_path) is None


def test_messaging_notice_outside_workspace_is_none(tmp_path: Path):
    assert messaging_notice(tmp_path) is None
