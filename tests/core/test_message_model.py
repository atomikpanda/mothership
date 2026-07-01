from datetime import datetime, timedelta, timezone

from mship.core.message import DecisionPayload, Message, Thread

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


# NOTE: `_dm` (not `_m`) is used below — the plan's snippet defines a helper
# named `_m` with a different signature (role, kind, text, decision, t) that
# would collide with and shadow the `_m(role, minute, *, kind)` helper used by
# the tests above. Renamed to avoid breaking the existing suite.
def _dm(role, kind="note", text="x", decision=None, t="2026-07-01T00:00:00+00:00"):
    return Message(id=text, thread_id="th", role=role, text=text,
                   created_at=datetime.fromisoformat(t), kind=kind, decision=decision)


def test_decision_payload_roundtrips():
    d = DecisionPayload(options=["File-per-thread", "SQLite"], recommended=0)
    m = _dm("agent", "decision", "How to store?", decision=d)
    back = Message.model_validate_json(m.model_dump_json())
    assert back.kind == "decision"
    assert back.decision.options == ["File-per-thread", "SQLite"]
    assert back.decision.recommended == 0 and back.decision.allow_free_text is True


def test_needs_decision_true_when_unanswered():
    th = Thread(id="t", subject="s", created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                messages=[_dm("agent", "decision", "pick", DecisionPayload(options=["a", "b"]))])
    assert th.needs_decision is True


def test_needs_decision_false_after_human_reply():
    th = Thread(id="t", subject="s", created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                messages=[_dm("agent", "decision", "pick", DecisionPayload(options=["a", "b"])),
                          _dm("human", "note", "a")])
    assert th.needs_decision is False
