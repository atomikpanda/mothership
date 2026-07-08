# tests/core/view/test_workitem_index.py
from datetime import datetime, timezone

from mship.core.workitem import WorkItem
from mship.core.spec import Spec
from mship.core.state import Task
from mship.core.message import Message, Thread
from mship.core.view.workitem_index import (
    Attention,
    WorkItemSummary,
    build_workitem_index,
    compute_attention,
    compute_phase,
)


def _now():
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _wi(**kw):
    base = dict(id="wi", title="t", workspace="ws", kind="feature",
                created_at=_now(), updated_at=_now())
    base.update(kw)
    return WorkItem(**base)


def _spec(status):
    return Spec(id="s", title="t", status=status, created_at=_now(), updated_at=_now())


def _task(*, finished=False, pr=False, blocked=False):
    return Task(
        slug="s1", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="b",
        finished_at=_now() if finished else None,
        pr_urls={"mothership": "http://pr"} if pr else {},
        blocked_reason="waiting" if blocked else None,
    )


def test_phase_override_wins():
    assert compute_phase(_wi(phase_override="done"), _spec("drafting"), [_task()]) == "done"


def test_no_children_is_inbox():
    assert compute_phase(_wi(), None, []) == "inbox"


def test_spec_status_maps_to_phase():
    assert compute_phase(_wi(), _spec("captured"), []) == "inbox"
    assert compute_phase(_wi(), _spec("drafting"), []) == "shaping"
    assert compute_phase(_wi(), _spec("needs_review"), []) == "shaping"
    assert compute_phase(_wi(), _spec("needs_clarification"), []) == "shaping"
    assert compute_phase(_wi(), _spec("approved"), []) == "ready"
    assert compute_phase(_wi(), _spec("dispatched"), []) == "in_flight"
    assert compute_phase(_wi(), _spec("implemented"), []) == "done"
    assert compute_phase(_wi(), _spec("archived"), []) == "done"


def test_tasks_dominate_spec():
    assert compute_phase(_wi(), _spec("approved"), [_task(finished=False)]) == "in_flight"
    # gc32 ac2 backstop: once every linked task is finished the work has landed,
    # so the item is done even when a PR is still recorded (spec status may lag).
    assert compute_phase(_wi(), _spec("approved"),
                         [_task(finished=True, pr=True)]) == "done"
    assert compute_phase(_wi(), _spec("approved"),
                         [_task(finished=True, pr=False)]) == "done"


def test_mixed_tasks_one_running_is_in_flight():
    assert compute_phase(_wi(), None,
                         [_task(finished=True, pr=True), _task(finished=False)]) == "in_flight"


# --- gc32 ac2: derivation backstop (all linked tasks finished/merged -> done) ---

def test_backstop_all_finished_derives_done_even_when_spec_lags():
    """The unattended-run case: PR merged directly (no `mship close`), so the task
    lingers finished with a PR while the spec is still `dispatched`. The item must
    still derive to `done` from the task-level landed signal, not sit in review."""
    tasks = [_task(finished=True, pr=True)]
    assert compute_phase(_wi(), _spec("dispatched"), tasks) == "done"


def test_backstop_multiple_tasks_all_finished_is_done():
    a = _task(finished=True, pr=True)
    b = _task(finished=True, pr=False)
    assert compute_phase(_wi(), _spec("dispatched"), [a, b]) == "done"


def test_backstop_does_not_fire_when_any_task_unfinished():
    """CRITICAL (top risk): a single still-in-progress task keeps the item out of
    done — the existing in_flight derivation is unchanged."""
    a = _task(finished=True, pr=True)
    b = _task(finished=False)
    assert compute_phase(_wi(), _spec("dispatched"), [a, b]) == "in_flight"


def test_backstop_requires_at_least_one_task():
    """No tasks -> the backstop cannot fire; derivation falls back to the spec."""
    assert compute_phase(_wi(), _spec("dispatched"), []) == "in_flight"
    assert compute_phase(_wi(), _spec("approved"), []) == "ready"


def _thread(*, needs_you=False):
    msgs = []
    if needs_you:
        msgs = [Message(id="m1", thread_id="t1", role="agent", text="?", created_at=_now(),
                        kind="needs_you")]
    return Thread(id="t1", subject="s", created_at=_now(), updated_at=_now(), messages=msgs)


def test_attention_clear_when_no_signals():
    att = compute_attention(_spec("approved"), [_task()], [])
    assert att == Attention(needs_approval=False, needs_decision=False, blocked=False,
                            needs_review=False, blocked_tasks=0, total_tasks=1)


def test_needs_approval_from_spec_needs_review():
    att = compute_attention(_spec("needs_review"), [], [])
    assert att.needs_approval is True


def test_blocked_counts_across_parallel_tasks():
    att = compute_attention(None, [_task(blocked=True), _task(), _task()], [])
    assert att.blocked is True
    assert att.blocked_tasks == 1 and att.total_tasks == 3


def test_needs_review_when_a_task_has_a_pr():
    assert compute_attention(None, [_task(pr=True)], []).needs_review is True


def test_needs_decision_from_thread_needs_you():
    assert compute_attention(None, [], [_thread(needs_you=True)]).needs_decision is True
    assert compute_attention(None, [], [_thread(needs_you=False)]).needs_decision is False


def test_attention_needs_decision_from_a_real_decision():
    from mship.core.message import DecisionPayload, Message, Thread
    from datetime import datetime, timezone
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    th = Thread(id="t1", subject="s", created_at=now, updated_at=now,
                messages=[Message(id="m", thread_id="t1", role="agent", text="?", created_at=now,
                                  kind="decision", decision=DecisionPayload(options=["a", "b"]))])
    att = compute_attention(None, [], [th])
    assert att.needs_decision is True


def test_build_index_populates_phase_and_attention():
    item = _wi(id="wi-1", spec_id="s", task_slugs=["s1"], thread_ids=["t1"])
    summaries = build_workitem_index(
        workitems=[item],
        specs_by_id={"s": _spec("approved")},
        tasks_by_slug={"s1": _task(blocked=True)},
        threads_by_id={"t1": _thread(needs_you=True)},
    )
    assert len(summaries) == 1
    s = summaries[0]
    assert isinstance(s, WorkItemSummary)
    assert s.id == "wi-1" and s.kind == "feature"
    assert s.phase == "in_flight"
    assert s.attention.blocked is True and s.attention.blocked_tasks == 1
    assert s.attention.needs_decision is True


def test_build_index_orders_active_before_done():
    active = _wi(id="active", updated_at=datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc))
    done = _wi(id="done", phase_override="done",
               updated_at=datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc))
    summaries = build_workitem_index([done, active], {}, {}, {})
    assert [s.id for s in summaries] == ["active", "done"]


def test_build_index_tolerates_missing_children():
    item = _wi(id="wi-x", spec_id="ghost", task_slugs=["missing"], thread_ids=["gone"])
    s = build_workitem_index([item], {}, {}, {})[0]
    assert s.phase == "inbox"
    assert s.attention.total_tasks == 0


def test_build_index_populates_unattended_true():
    item = _wi(id="wi-u", unattended=True)
    s = build_workitem_index([item], {}, {}, {})[0]
    assert s.unattended is True


def test_build_index_unattended_defaults_false():
    item = _wi(id="wi-default")
    s = build_workitem_index([item], {}, {}, {})[0]
    assert s.unattended is False
