from __future__ import annotations
from datetime import datetime, timezone

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_store import SpecStore
from mship.core.view.actions import approve_spec_by_id, request_changes_by_id


def _dt(): return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _store(tmp_path, **over):
    store = SpecStore(tmp_path / "specs")
    base = dict(id="s1", title="t", status="needs_review", created_at=_dt(), updated_at=_dt(),
                body="b\n", acceptance_criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="approved")],
                open_questions=[])
    base.update(over)
    store.save(Spec(**base))
    return store


def test_approve_ok_reflects_new_status(tmp_path):
    store = _store(tmp_path)
    out = approve_spec_by_id(store, "s1")
    assert out.ok and out.new_status == "approved"
    assert store.find_by_id("s1").status == "approved"


def test_approve_noop_when_not_needs_review(tmp_path):
    store = _store(tmp_path, status="approved")
    out = approve_spec_by_id(store, "s1")
    assert not out.ok and "not awaiting review" in out.message
    assert store.find_by_id("s1").status == "approved"


def test_approve_reports_open_questions_gate(tmp_path):
    store = _store(tmp_path, open_questions=[OpenQuestion(id="q1", text="?", answer=None)])
    out = approve_spec_by_id(store, "s1")
    assert not out.ok and "q1" in out.message
    assert store.find_by_id("s1").status == "needs_review"


def test_request_changes_needs_reason_and_writes_draft(tmp_path):
    store = _store(tmp_path)
    assert not request_changes_by_id(store, "s1", "   ").ok         # empty reason rejected
    out = request_changes_by_id(store, "s1", "tighten AC2")
    assert out.ok and out.new_status == "draft"
    assert store.find_by_id("s1").clarification_reason == "tighten AC2"


def test_missing_spec_is_safe(tmp_path):
    store = SpecStore(tmp_path / "specs")
    assert not approve_spec_by_id(store, "nope").ok
    assert not approve_spec_by_id(store, None).ok
