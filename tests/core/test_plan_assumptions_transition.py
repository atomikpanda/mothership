import pytest
from mship.core.assumptions import SEED_ROWS
from mship.core.plan_check import Flag, PlanCheckResult, PlanCheckStore, assumptions_hash, plan_hash
from mship.core.plan_assumptions_transition import (
    approve_flag, NoStoredCheck, StaleCheck, UnknownAxis,
)

PLAN_TEXT = "p\n"
ROWS = list(SEED_ROWS)


def _seed(tmp_path, flags, plan_text=PLAN_TEXT, rows=ROWS):
    store = PlanCheckStore(tmp_path / ".mothership")
    store.save(PlanCheckResult(
        task_slug="t", plan_hash=plan_hash(plan_text), assumptions_hash=assumptions_hash(rows),
        verdicts=[], flags=flags,
    ))
    return store


def test_approve_flag_marks_the_axis_and_records_operator(tmp_path):
    store = _seed(tmp_path, [Flag(axis="repo topology", source="checker", reason="gap")])
    result = approve_flag(
        store, "t", "Repo  Topology", reason="ok", approved_by="operator",
        plan_text=PLAN_TEXT, rows=ROWS,
    )
    match = [f for f in result.flags if f.axis == "repo topology"]
    assert len(match) == 1
    assert match[0].approved is True
    assert match[0].approved_by == "operator"
    assert match[0].approved_reason == "ok"
    # persisted
    assert store.get("t").flags[0].approved is True


def test_approve_flag_is_idempotent_on_already_approved(tmp_path):
    store = _seed(tmp_path, [Flag(axis="repo topology", source="checker", reason="g", approved=True,
                                   approved_by="operator")])
    result = approve_flag(
        store, "t", "repo topology", reason="again", approved_by="operator",
        plan_text=PLAN_TEXT, rows=ROWS,
    )
    match = [f for f in result.flags if f.axis == "repo topology"]
    assert match[0].approved is True
    assert match[0].approved_reason is None  # unchanged; not overwritten by the no-op


def test_approve_flag_unknown_axis_raises(tmp_path):
    store = _seed(tmp_path, [Flag(axis="repo topology", source="checker", reason="g")])
    with pytest.raises(UnknownAxis):
        approve_flag(
            store, "t", "not a row", reason=None, approved_by="operator",
            plan_text=PLAN_TEXT, rows=ROWS,
        )


def test_approve_flag_no_stored_check_raises(tmp_path):
    store = PlanCheckStore(tmp_path / ".mothership")
    with pytest.raises(NoStoredCheck):
        approve_flag(
            store, "t", "repo topology", reason=None, approved_by="operator",
            plan_text=PLAN_TEXT, rows=ROWS,
        )


def test_approve_flag_raises_stale_check_when_plan_changed_since_recorded(tmp_path):
    store = _seed(tmp_path, [Flag(axis="repo topology", source="checker", reason="gap")])
    with pytest.raises(StaleCheck):
        approve_flag(
            store, "t", "repo topology", reason=None, approved_by="operator",
            plan_text="a different plan entirely\n", rows=ROWS,
        )
    # the stale approve must not have mutated the stored check
    assert store.get("t").flags[0].approved is False


def test_approve_flag_raises_stale_check_when_assumptions_changed_since_recorded(tmp_path):
    store = _seed(tmp_path, [Flag(axis="repo topology", source="checker", reason="gap")])
    changed_rows = ROWS[1:]  # a row was removed from the set since the check ran
    with pytest.raises(StaleCheck):
        approve_flag(
            store, "t", "repo topology", reason=None, approved_by="operator",
            plan_text=PLAN_TEXT, rows=changed_rows,
        )
