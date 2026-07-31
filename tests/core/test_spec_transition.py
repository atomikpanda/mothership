from __future__ import annotations
import json
from datetime import datetime, timezone

import pytest

from mship.core.log import LogManager
from mship.core.spec import AcceptanceCriterion, InvalidTransition, OpenQuestion, Spec
from mship.core.spec_store import SpecStore
from mship.core.spec_transition import ApprovalBlocked, approve_spec, request_changes_spec


def _dt():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _reviewable(**over) -> Spec:
    base = dict(
        id="s1", title="t", status="needs_review", created_at=_dt(), updated_at=_dt(),
        body="b\n", acceptance_criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="approved")],
        open_questions=[],
    )
    base.update(over)
    return Spec(**base)


def test_approve_transitions_and_persists_via_store(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _reviewable()
    store.save(spec)
    approve_spec(spec, store)
    reloaded = store.find_by_id("s1")
    assert reloaded.status == "approved"
    assert reloaded.clarification_reason is None


def test_approve_blocked_by_open_questions_does_not_write(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _reviewable(open_questions=[OpenQuestion(id="q1", text="?", answer=None)])
    store.save(spec)
    with pytest.raises(ApprovalBlocked) as e:
        approve_spec(spec, store)
    assert "q1" in "; ".join(e.value.blockers)
    assert store.find_by_id("s1").status == "needs_review"


def test_request_changes_sends_to_draft_with_reason(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _reviewable()
    store.save(spec)
    log = LogManager(tmp_path / "logs")
    request_changes_spec(spec, store, "tighten AC2", log_manager=log, actor="alice")
    reloaded = store.find_by_id("s1")
    assert reloaded.status == "draft"
    assert reloaded.clarification_reason == "tighten AC2"


def test_request_changes_records_durable_rejection(tmp_path):
    """P1 (#458): the durable rejection record must be written by
    `request_changes_spec` itself, so every caller (CLI/serve/view) gets it
    automatically instead of remembering a separate `record_rejection` call."""
    store = SpecStore(tmp_path / "specs")
    spec = _reviewable()
    store.save(spec)
    log = LogManager(tmp_path / "logs")
    request_changes_spec(spec, store, "tighten AC2", log_manager=log, actor="alice")
    rejected = [e for e in log.read("s1") if e.action == "rejected"]
    assert len(rejected) == 1
    payload = json.loads(rejected[0].message)
    assert payload == {"actor": "alice", "reason": "tighten AC2"}


def test_request_changes_rejects_empty_reason_before_any_write(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _reviewable()
    store.save(spec)
    log = LogManager(tmp_path / "logs")
    with pytest.raises(ValueError):
        request_changes_spec(spec, store, "   ", log_manager=log, actor="alice")
    assert store.find_by_id("s1").status == "needs_review"
    assert log.read("s1") == []


def test_approve_illegal_status_raises_invalid_transition(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _reviewable(status="draft")
    store.save(spec)
    with pytest.raises(InvalidTransition):
        approve_spec(spec, store)
