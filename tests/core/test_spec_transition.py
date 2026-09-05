from __future__ import annotations
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from mship.core import spec_key
from mship.core.inbox import InboxAction
from mship.core.log import LogManager
from mship.core.spec import AcceptanceCriterion, InvalidTransition, OpenQuestion, Spec
from mship.core.spec_storage import SpecLocked, SpecStorage
from mship.core.spec_store import (
    SpecArtifactConflict,
    SpecParseError,
    SpecRepresentationMismatch,
    SpecStore,
)
from mship.core.spec_transition import (
    ApprovalBlocked,
    SpecRevisionConflict,
    approve_spec,
    request_changes_spec,
)
from mship.core.spec_draft import SpecDraft, apply_draft_transaction


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


def _save_newer_revision(store: SpecStore) -> Spec:
    current = store.find_by_id("s1")
    assert current is not None
    current.updated_at = datetime(2026, 7, 2, tzinfo=timezone.utc)
    store.save(current)
    return current


def _assert_spec_lock_released(store: SpecStore, spec_id: str) -> None:
    acquired = threading.Event()

    def acquire_lock() -> None:
        with store.locked(spec_id):
            acquired.set()

    thread = threading.Thread(target=acquire_lock)
    thread.start()
    thread.join(timeout=5)
    assert acquired.is_set()
    assert not thread.is_alive()


def test_approve_rejects_a_stale_loaded_spec_without_mutating_either_revision(tmp_path):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    actor_a = store.find_by_id("s1")
    assert actor_a is not None
    actor_b = _save_newer_revision(store)
    stale_before = actor_a.model_dump()
    current_before = actor_b.model_dump()

    with pytest.raises(SpecRevisionConflict) as raised:
        approve_spec(actor_a, store)

    conflict = raised.value
    assert conflict.spec_id == "s1"
    assert conflict.expected_updated_at == _dt()
    assert conflict.current_updated_at == datetime(2026, 7, 2, tzinfo=timezone.utc)
    assert str(conflict) == "spec revision conflict for 's1'; reload and retry"
    assert actor_a.model_dump() == stale_before
    assert store.find_by_id("s1").model_dump() == current_before



def test_approve_rejects_a_stale_review_clearance_before_evaluating_blockers(tmp_path):
    store = SpecStore(tmp_path / "specs")
    store.save(
        _reviewable(open_questions=[OpenQuestion(id="q1", text="?", answer=None)])
    )
    stale = store.find_by_id("s1")
    current = store.find_by_id("s1")
    assert stale is not None
    assert current is not None
    current.open_questions[0].answer = "answered"
    store.save(current)

    with pytest.raises(SpecRevisionConflict):
        approve_spec(stale, store)

    persisted = store.find_by_id("s1")
    assert persisted is not None
    assert persisted.open_questions[0].answer == "answered"
    assert persisted.status == "needs_review"


def test_request_changes_rejects_a_stale_status_before_evaluating_transition(tmp_path):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable(status="draft"))
    stale = store.find_by_id("s1")
    current = store.find_by_id("s1")
    assert stale is not None
    assert current is not None
    current.status = "needs_review"
    store.save(current)
    log = LogManager(tmp_path / "logs")

    with pytest.raises(SpecRevisionConflict):
        request_changes_spec(
            stale, store, "tighten AC2", log_manager=log, actor="alice"
        )

    persisted = store.find_by_id("s1")
    assert persisted is not None
    assert persisted.status == "needs_review"
    assert log.read("s1") == []

def test_request_changes_rejects_a_stale_loaded_spec_without_writing_a_rejection(tmp_path):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    actor_a = store.find_by_id("s1")
    assert actor_a is not None
    actor_b = _save_newer_revision(store)
    stale_before = actor_a.model_dump()
    current_before = actor_b.model_dump()
    log = LogManager(tmp_path / "logs")

    with pytest.raises(SpecRevisionConflict):
        request_changes_spec(
            actor_a, store, "tighten AC2", log_manager=log, actor="alice"
        )

    assert actor_a.model_dump() == stale_before
    assert store.find_by_id("s1").model_dump() == current_before
    assert log.read("s1") == []



def test_approve_rejects_same_timestamp_work_item_mutation_without_losing_association(tmp_path):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    stale = store.find_by_id("s1")
    current = store.find_by_id("s1")
    assert stale is not None
    assert current is not None
    current.work_item_id = "wi-current"
    store.save(current)

    with pytest.raises(SpecRevisionConflict) as raised:
        approve_spec(stale, store)

    assert raised.value.expected_updated_at == _dt()
    assert raised.value.current_updated_at == _dt()
    persisted = store.find_by_id("s1")
    assert persisted is not None
    assert persisted.work_item_id == "wi-current"
    assert persisted.status == "needs_review"


def test_request_changes_rejects_same_timestamp_work_item_mutation_without_losing_association(
    tmp_path,
):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    stale = store.find_by_id("s1")
    current = store.find_by_id("s1")
    assert stale is not None
    assert current is not None
    current.work_item_id = "wi-current"
    store.save(current)
    log = LogManager(tmp_path / "logs")

    with pytest.raises(SpecRevisionConflict) as raised:
        request_changes_spec(
            stale, store, "tighten AC2", log_manager=log, actor="alice"
        )

    assert raised.value.expected_updated_at == _dt()
    assert raised.value.current_updated_at == _dt()
    persisted = store.find_by_id("s1")
    assert persisted is not None
    assert persisted.work_item_id == "wi-current"
    assert persisted.status == "needs_review"
    assert log.read("s1") == []


def test_approve_ignores_canonical_inbox_refresh_without_a_revision_conflict(tmp_path):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    stale = store.find_by_id("s1")
    assert stale is not None
    inbox_action: InboxAction = "pin"
    inbox_mutation_id = "inbox-pin-1"
    inbox_now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    store.mutate_inbox("s1", inbox_action, inbox_mutation_id, inbox_now)

    approve_spec(stale, store)

    persisted = store.find_by_id("s1")
    assert persisted is not None
    assert persisted.status == "approved"
    assert persisted.inbox.pinned is True


def test_approve_propagates_malformed_current_artifact_and_releases_lock(tmp_path):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    actor = store.find_by_id("s1")
    assert actor is not None
    artifact_path = store.path_for(actor)
    original_text = artifact_path.read_text()
    artifact_path.write_text("not a spec")

    with pytest.raises(SpecParseError) as raised:
        approve_spec(actor, store)

    assert type(raised.value) is SpecParseError
    artifact_path.write_text(original_text)
    _assert_spec_lock_released(store, actor.id)


def test_approve_propagates_duplicate_current_artifact_and_releases_lock(tmp_path):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    actor = store.find_by_id("s1")
    assert actor is not None
    artifact_path = store.path_for(actor)
    duplicate_path = artifact_path.with_name(artifact_path.name + ".enc")
    duplicate_path.write_text(artifact_path.read_text())

    with pytest.raises(SpecArtifactConflict) as raised:
        approve_spec(actor, store)

    assert type(raised.value) is SpecArtifactConflict
    duplicate_path.unlink()
    _assert_spec_lock_released(store, actor.id)


def test_approve_propagates_locked_current_artifact_and_releases_lock(tmp_path):
    storage = SpecStorage(
        tmp_path / "specs", mode="encrypted", workspace_root=tmp_path
    )
    store = SpecStore(tmp_path / "specs", storage=storage)
    store.save(_reviewable())
    actor = store.find_by_id("s1")
    assert actor is not None
    key_path = spec_key.keyfile_path(tmp_path)
    key = key_path.read_bytes()
    key_path.unlink()

    with pytest.raises(SpecLocked) as raised:
        approve_spec(actor, store)

    assert type(raised.value) is SpecLocked
    key_path.write_bytes(key)
    _assert_spec_lock_released(store, actor.id)


def test_approve_propagates_save_time_representation_mismatch(tmp_path):
    plaintext_store = SpecStore(tmp_path / "specs")
    plaintext_store.save(_reviewable())
    actor = plaintext_store.find_by_id("s1")
    assert actor is not None
    encrypted_store = SpecStore(
        tmp_path / "specs",
        storage=SpecStorage(
            tmp_path / "specs", mode="encrypted", workspace_root=tmp_path
        ),
    )

    with pytest.raises(SpecRepresentationMismatch, match="migrate storage"):
        approve_spec(actor, encrypted_store)

    persisted = plaintext_store.find_by_id("s1")
    assert persisted is not None
    assert persisted.status == "needs_review"


@pytest.mark.parametrize(
    "failure",
    [SpecLocked("s1"), OSError("disk full")],
    ids=["locked", "write-error"],
)
def test_approve_propagates_save_time_storage_failures_and_releases_lock(
    tmp_path, monkeypatch, failure
):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    actor = store.find_by_id("s1")
    assert actor is not None

    def fail_write(*_args):
        raise failure

    monkeypatch.setattr(store._storage, "write", fail_write)

    with pytest.raises(type(failure)) as raised:
        approve_spec(actor, store)

    assert raised.value is failure
    persisted = store.find_by_id("s1")
    assert persisted is not None
    assert persisted.status == "needs_review"
    acquired = threading.Event()

    def acquire_lock():
        with store.locked(actor.id):
            acquired.set()

    thread = threading.Thread(target=acquire_lock)
    thread.start()
    thread.join(timeout=5)
    assert acquired.is_set()
    assert not thread.is_alive()

@pytest.mark.parametrize(
    "failure",
    [SpecLocked("s1"), OSError("disk full")],
    ids=["locked", "write-error"],
)
def test_request_changes_propagates_save_failures_without_writing_a_rejection(
    tmp_path, monkeypatch, failure
):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    actor = store.find_by_id("s1")
    assert actor is not None
    log = LogManager(tmp_path / "logs")

    def fail_write(*_args):
        raise failure

    monkeypatch.setattr(store._storage, "write", fail_write)

    with pytest.raises(type(failure)) as raised:
        request_changes_spec(
            actor, store, "tighten AC2", log_manager=log, actor="alice"
        )

    assert raised.value is failure
    persisted = store.find_by_id("s1")
    assert persisted is not None
    assert persisted.status == "needs_review"
    assert log.read("s1") == []


def test_request_changes_rolls_back_the_persisted_spec_when_journal_append_fails(
    tmp_path, monkeypatch
):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    actor = store.find_by_id("s1")
    original = store.find_by_id("s1")
    assert actor is not None
    assert original is not None
    log = LogManager(tmp_path / "logs")
    journal_error = OSError("journal unavailable")
    status_at_append: list[str] = []

    def fail_append(*_args, **_kwargs):
        persisted = store.find_by_id("s1")
        assert persisted is not None
        status_at_append.append(persisted.status)
        raise journal_error

    monkeypatch.setattr(log, "append", fail_append)

    with pytest.raises(OSError) as raised:
        request_changes_spec(
            actor, store, "tighten AC2", log_manager=log, actor="alice"
        )

    assert raised.value is journal_error
    assert status_at_append == ["draft"]
    persisted = store.find_by_id("s1")
    assert persisted is not None
    assert persisted.model_dump() == original.model_dump()



def test_request_changes_serializes_same_revision_and_logs_only_the_winner(
    tmp_path, monkeypatch
):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    actor_a = store.find_by_id("s1")
    actor_b = store.find_by_id("s1")
    assert actor_a is not None
    assert actor_b is not None
    log = LogManager(tmp_path / "logs")
    write_started = threading.Event()
    release_write = threading.Event()
    actor_b_started = threading.Event()
    release_stale_read = threading.Event()
    outcomes: list[Exception | None] = []
    original_locked = store.locked
    original_resolve_artifact = store.resolve_artifact
    original_save_unlocked = store._save_unlocked

    @contextmanager
    def signal_actor_b_lock(spec_id):
        if threading.current_thread().name == "actor-b":
            actor_b_started.set()
        with original_locked(spec_id) as artifact:
            yield artifact

    def block_actor_b_stale_read(spec_id):
        artifact = original_resolve_artifact(spec_id)
        if threading.current_thread().name == "actor-b":
            actor_b_started.set()
            assert release_stale_read.wait(timeout=5)
        return artifact

    def block_actor_a_before_write(spec, artifact=None):
        if spec is actor_a:
            write_started.set()
            assert release_write.wait(timeout=5)
        return original_save_unlocked(spec, artifact)

    monkeypatch.setattr(store, "locked", signal_actor_b_lock)
    monkeypatch.setattr(store, "resolve_artifact", block_actor_b_stale_read)
    monkeypatch.setattr(store, "_save_unlocked", block_actor_a_before_write)

    def transition(spec, actor):
        try:
            request_changes_spec(
                spec, store, "tighten AC2", log_manager=log, actor=actor, now=_dt()
            )
        except Exception as exc:
            outcomes.append(exc)
        else:
            outcomes.append(None)

    thread_a = threading.Thread(target=transition, args=(actor_a, "alice"))
    thread_b = threading.Thread(
        target=transition, args=(actor_b, "bob"), name="actor-b"
    )
    thread_a.start()
    assert write_started.wait(timeout=5)
    thread_b.start()
    assert actor_b_started.wait(timeout=5)
    release_write.set()
    release_stale_read.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert outcomes.count(None) == 1
    assert sum(isinstance(outcome, SpecRevisionConflict) for outcome in outcomes) == 1
    reloaded = store.find_by_id("s1")
    assert reloaded is not None
    assert reloaded.clarification_reason == "tighten AC2"
    assert reloaded.updated_at > _dt()
    rejected = [event for event in log.read("s1") if event.action == "rejected"]
    assert len(rejected) == 1
    assert json.loads(rejected[0].message) == {
        "actor": "alice",
        "reason": "tighten AC2",
    }

def test_approve_transitions_and_persists_via_store(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _reviewable()
    store.save(spec)
    approve_spec(spec, store)
    reloaded = store.find_by_id("s1")
    assert reloaded.status == "approved"
    assert reloaded.clarification_reason is None
    assert reloaded.updated_at > _dt()

def test_approve_advances_a_legacy_naive_persisted_revision(tmp_path):
    legacy_updated_at = datetime(2026, 7, 1)
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable(updated_at=legacy_updated_at))
    spec = store.find_by_id("s1")
    assert spec is not None
    assert spec.updated_at == legacy_updated_at
    assert spec.updated_at.tzinfo is None


    approve_spec(spec, store)

    reloaded = store.find_by_id("s1")
    assert reloaded is not None
    assert reloaded.status == "approved"
    assert reloaded.updated_at.tzinfo is None
    assert reloaded.updated_at > legacy_updated_at



def test_approve_does_not_restore_a_snapshot_stale_after_apply(tmp_path):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    stale = store.find_by_id("s1")
    assert stale is not None

    apply_draft_transaction(
        store,
        "s1",
        SpecDraft(
            problem="New problem",
            user_story="New story",
            approach="New approach",
            acceptance_criteria=["replacement"],
        ),
        bypass_status_gate=True,
        discard_review=True,
    )
    with pytest.raises(SpecRevisionConflict):
        approve_spec(stale, store, bypass_gate=True)

    persisted = store.find_by_id("s1")
    assert persisted is not None
    assert persisted.status == "needs_review"
    assert persisted.acceptance_criteria[0].text == "replacement"
    assert "New approach" in persisted.body

def test_approve_blocked_by_open_questions_does_not_write(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _reviewable(open_questions=[OpenQuestion(id="q1", text="?", answer=None)])
    store.save(spec)
    with pytest.raises(ApprovalBlocked) as e:
        approve_spec(spec, store)
    assert "q1" in "; ".join(e.value.blockers)
    assert store.find_by_id("s1").status == "needs_review"



def test_request_changes_does_not_restore_a_snapshot_stale_after_apply(tmp_path):
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable())
    stale = store.find_by_id("s1")
    assert stale is not None
    log = LogManager(tmp_path / "logs")

    apply_draft_transaction(
        store,
        "s1",
        SpecDraft(
            problem="New problem",
            user_story="New story",
            approach="New approach",
            acceptance_criteria=["replacement"],
        ),
        bypass_status_gate=True,
        discard_review=True,
    )
    with pytest.raises(SpecRevisionConflict):
        request_changes_spec(stale, store, "tighten scope", log_manager=log, actor="alice")

    persisted = store.find_by_id("s1")
    assert persisted is not None
    assert persisted.status == "needs_review"
    assert persisted.acceptance_criteria[0].text == "replacement"
    assert "New approach" in persisted.body
    assert log.read("s1") == []

def test_request_changes_sends_to_draft_with_reason(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _reviewable()
    store.save(spec)
    log = LogManager(tmp_path / "logs")
    request_changes_spec(spec, store, "tighten AC2", log_manager=log, actor="alice")
    reloaded = store.find_by_id("s1")
    assert reloaded.status == "draft"
    assert reloaded.clarification_reason == "tighten AC2"

def test_request_changes_advances_a_legacy_naive_persisted_revision(tmp_path):
    legacy_updated_at = datetime(2026, 7, 1)
    store = SpecStore(tmp_path / "specs")
    store.save(_reviewable(updated_at=legacy_updated_at))
    spec = store.find_by_id("s1")
    assert spec is not None
    assert spec.updated_at == legacy_updated_at
    assert spec.updated_at.tzinfo is None

    log = LogManager(tmp_path / "logs")

    request_changes_spec(
        spec, store, "tighten AC2", log_manager=log, actor="alice", now=_dt()
    )

    reloaded = store.find_by_id("s1")
    assert reloaded is not None
    assert reloaded.status == "draft"
    assert reloaded.clarification_reason == "tighten AC2"
    assert reloaded.updated_at.tzinfo is None
    assert reloaded.updated_at > legacy_updated_at
    rejected = [event for event in log.read("s1") if event.action == "rejected"]
    assert len(rejected) == 1
    assert json.loads(rejected[0].message) == {
        "actor": "alice",
        "reason": "tighten AC2",
    }


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
