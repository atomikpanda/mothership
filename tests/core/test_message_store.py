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


def test_link_spec_sets_spec_id(tmp_path):
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="x", text="hi", now=now)
    s.link_spec(t.id, "my-spec")
    assert s.get(t.id).spec_id == "my-spec"


def test_link_spec_unknown_thread_raises(tmp_path):
    with pytest.raises(KeyError):
        _store(tmp_path).link_spec("nope", "s")


def test_link_spec_with_now_advances_updated_at(tmp_path):
    created = datetime(2026, 6, 23, tzinfo=timezone.utc)
    linked = datetime(2026, 6, 24, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="x", text="hi", now=created)
    s.link_spec(t.id, "my-spec", now=linked)
    refreshed = s.get(t.id)
    assert refreshed.spec_id == "my-spec"
    assert refreshed.updated_at == linked  # linking bubbles the thread up in list()


def test_append_defaults_to_note_kind(tmp_path):
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="x", text="q", now=now)
    s.append(t.id, "agent", "fyi", now)
    got = s.get(t.id)
    assert got.messages[-1].kind == "note"
    assert got.needs_you is False


def test_append_needs_you_kind_flags_thread(tmp_path):
    now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="x", text="q", now=now)
    s.append(t.id, "agent", "look at this", now, kind="needs_you")
    got = s.get(t.id)
    assert got.messages[-1].kind == "needs_you"
    assert got.needs_you is True


def test_mark_seen_advances_cursor_and_clears_unseen(tmp_path):
    from datetime import timedelta
    base = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="x", text="hi", now=base)
    s.append(t.id, "agent", "fyi", base + timedelta(minutes=1))
    assert s.get(t.id).unseen is True
    s.mark_seen(t.id, base + timedelta(minutes=2))
    assert s.get(t.id).unseen is False
    assert s.get(t.id).seen_at == base + timedelta(minutes=2)


def test_mark_seen_is_monotonic(tmp_path):
    from datetime import timedelta
    base = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="x", text="hi", now=base)
    s.mark_seen(t.id, base + timedelta(minutes=5))
    s.mark_seen(t.id, base + timedelta(minutes=1))  # older — must not regress
    assert s.get(t.id).seen_at == base + timedelta(minutes=5)


def test_mark_seen_unknown_thread_raises(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(KeyError):
        s.mark_seen("nope", datetime(2026, 6, 30, tzinfo=timezone.utc))


def test_append_decision_roundtrips(tmp_path):
    from mship.core.message import DecisionPayload
    s = _store(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    th = s.create_thread("s", "hi", now)
    s.append(th.id, "agent", "How to store?", now, kind="decision",
              decision=DecisionPayload(options=["a", "b"], recommended=1))
    got = s.get(th.id)
    assert got.messages[-1].kind == "decision"
    assert got.messages[-1].decision.options == ["a", "b"]
    assert got.needs_decision is True
