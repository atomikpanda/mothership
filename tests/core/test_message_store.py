from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from mship.core.message_store import MessageStore


def _store(tmp_path: Path) -> MessageStore:
    return MessageStore(tmp_path / ".mothership" / "messages")


def test_create_thread_persists_with_first_human_message(tmp_path):
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="Idea", text="build a thing", now=now)
    assert t.subject == "Idea"
    assert [m.role for m in t.messages] == ["human"]
    assert t.messages[0].text == "build a thing"
    assert t.awaiting_reply is True
    # round-trips from disk
    loaded = s.get(t.id)
    assert loaded is not None and loaded.messages[0].text == "build a thing"


def test_append_agent_clears_awaiting_then_human_reraises(tmp_path):
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="x", text="hi", now=now)
    s.append(t.id, "agent", "drafted it", now + timedelta(minutes=1))
    assert s.get(t.id).awaiting_reply is False
    s.append(t.id, "human", "one more", now + timedelta(minutes=2))
    got = s.get(t.id)
    assert got.awaiting_reply is True
    assert got.updated_at == now + timedelta(minutes=2)
    assert [m.role for m in got.messages] == ["human", "agent", "human"]


def test_append_unknown_thread_raises(tmp_path):
    with pytest.raises(KeyError):
        _store(tmp_path).append("nope", "agent", "x", datetime.now(timezone.utc))


def test_list_sorted_by_updated_desc(tmp_path):
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    s = _store(tmp_path)
    a = s.create_thread(subject="a", text="a", now=now)
    b = s.create_thread(subject="b", text="b", now=now + timedelta(minutes=5))
    assert [t.id for t in s.list()] == [b.id, a.id]


def test_get_missing_is_none(tmp_path):
    assert _store(tmp_path).get("missing") is None


def test_unsafe_thread_id_rejected(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s._path("../escape")


def test_awaiting_reply_is_serialized(tmp_path):
    # @computed_field: awaiting_reply must appear in model_dump()/JSON, not just as a property.
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="x", text="hi", now=now)
    assert s.get(t.id).model_dump()["awaiting_reply"] is True
    s.append(t.id, "agent", "done", now)
    assert s.get(t.id).model_dump()["awaiting_reply"] is False
