from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mship.core.message import Message, Thread
from mship.core.message_wait import changed_since, wait_for_change, WaitResult

T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _thread(tid: str, updated: datetime, role: str = "human") -> Thread:
    return Thread(
        id=tid, subject=tid, created_at=updated, updated_at=updated,
        messages=[Message(id=f"{tid}-m", thread_id=tid, role=role, text="x", created_at=updated)],
    )


def test_changed_since_filters_newer_and_reports_cursor():
    threads = [_thread("a", T0), _thread("b", T0 + timedelta(seconds=5))]
    changed, cursor = changed_since(threads, T0)
    assert [t.id for t in changed] == ["b"]          # only strictly-newer
    assert cursor == T0 + timedelta(seconds=5)        # high-water mark


def test_changed_since_empty_when_nothing_newer():
    threads = [_thread("a", T0)]
    changed, cursor = changed_since(threads, T0 + timedelta(seconds=10))
    assert changed == []
    assert cursor == T0 + timedelta(seconds=10)       # never goes backwards


def test_changed_since_empty_store():
    changed, cursor = changed_since([], T0)
    assert changed == [] and cursor == T0


def test_wait_returns_when_a_hit_appears_on_a_later_poll():
    # load_fn returns nothing twice, then an awaiting thread — no real sleeping.
    polls = [[], [], [_thread("a", T0 + timedelta(seconds=1))]]
    calls = {"n": 0}
    def load_fn():
        i = min(calls["n"], len(polls) - 1); calls["n"] += 1; return polls[i]
    clock = {"t": 0.0}
    def now_fn(): return clock["t"]
    def sleep_fn(d): clock["t"] += d
    res = wait_for_change(load_fn, since=T0, timeout=100.0,
                          now_fn=now_fn, sleep_fn=sleep_fn, interval=1.0)
    assert isinstance(res, WaitResult)
    assert res.timed_out is False
    assert [t.id for t in res.threads] == ["a"]
    assert res.cursor == T0 + timedelta(seconds=1)


def test_wait_times_out_with_empty_result():
    clock = {"t": 0.0}
    res = wait_for_change(lambda: [], since=T0, timeout=3.0,
                          now_fn=lambda: clock["t"],
                          sleep_fn=lambda d: clock.__setitem__("t", clock["t"] + d),
                          interval=1.0)
    assert res.timed_out is True
    assert res.threads == []
    assert res.cursor == T0


def test_wait_predicate_filters_hits_but_cursor_still_advances():
    # An agent-role change bumps updated_at but is NOT a hit (predicate=awaiting).
    agent = _thread("a", T0 + timedelta(seconds=1), role="agent")
    clock = {"t": 0.0}
    res = wait_for_change(lambda: [agent], since=T0, timeout=2.0,
                          predicate=lambda t: t.awaiting_reply,
                          now_fn=lambda: clock["t"],
                          sleep_fn=lambda d: clock.__setitem__("t", clock["t"] + d),
                          interval=1.0)
    assert res.timed_out is True            # agent message is not a hit
    assert res.threads == []
    assert res.cursor == T0 + timedelta(seconds=1)   # cursor advanced past it
