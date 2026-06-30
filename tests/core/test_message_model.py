from datetime import datetime, timedelta, timezone

from mship.core.message import Message, Thread

BASE = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _thread(*msgs: Message, seen_at: datetime | None = None) -> Thread:
    return Thread(
        id="x", subject="s", created_at=BASE, updated_at=BASE,
        messages=list(msgs), seen_at=seen_at,
    )


def _m(role: str, minute: int, *, kind: str = "note") -> Message:
    return Message(
        id=f"m{minute}", thread_id="x", role=role, text=f"msg {minute}",
        created_at=BASE + timedelta(minutes=minute), kind=kind,
    )


def test_message_kind_defaults_to_note():
    assert _m("agent", 1).kind == "note"


def test_kind_round_trips_and_legacy_json_defaults_to_note():
    m = _m("agent", 1, kind="needs_you")
    assert Message.model_validate_json(m.model_dump_json()).kind == "needs_you"
    legacy = '{"id":"m1","thread_id":"x","role":"agent","text":"hi","created_at":"2026-06-30T12:01:00+00:00"}'
    assert Message.model_validate_json(legacy).kind == "note"


def test_needs_you_true_for_unanswered_needs_you():
    t = _thread(_m("human", 0), _m("agent", 1, kind="needs_you"))
    assert t.needs_you is True


def test_needs_you_false_for_plain_note():
    t = _thread(_m("human", 0), _m("agent", 1))
    assert t.needs_you is False


def test_needs_you_persists_after_a_followup_note():
    t = _thread(_m("human", 0), _m("agent", 1, kind="needs_you"), _m("agent", 2))
    assert t.needs_you is True


def test_needs_you_clears_after_human_reply():
    t = _thread(_m("human", 0), _m("agent", 1, kind="needs_you"), _m("human", 2))
    assert t.needs_you is False


def test_unseen_true_when_agent_newer_than_seen_cursor():
    t = _thread(_m("human", 0), _m("agent", 1), seen_at=None)
    assert t.unseen is True


def test_unseen_false_once_seen_cursor_advances_past_latest_agent():
    t = _thread(_m("human", 0), _m("agent", 1), seen_at=BASE + timedelta(minutes=2))
    assert t.unseen is False


def test_unseen_false_when_no_agent_messages():
    t = _thread(_m("human", 0))
    assert t.unseen is False


def test_needs_you_and_unseen_are_serialized():
    t = _thread(_m("human", 0), _m("agent", 1, kind="needs_you"))
    dumped = t.model_dump()
    assert dumped["needs_you"] is True
    assert dumped["unseen"] is True
