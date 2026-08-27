from __future__ import annotations
import subprocess
from datetime import datetime, timezone

from mship.core.log import LogManager
from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_store import SpecStore
from mship.core.view import actions
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


def test_request_changes_by_id_writes_durable_rejection_record(tmp_path):
    """P1 (#458): the TUI request-changes path (`mship view queue/spec/
    workitem`) must record a durable rejection just like CLI/serve — before
    this fix, `request_changes_by_id` transitioned the spec without ever
    calling `record_rejection`, silently dropping a whole rejection path
    from the journal."""
    import json

    from mship.core.log import LogManager

    store = _store(tmp_path)
    out = request_changes_by_id(store, "s1", "tighten AC2")
    assert out.ok and out.new_status == "draft"

    log = LogManager(tmp_path / ".mothership" / "logs")
    rejected = [e for e in log.read("s1") if e.action == "rejected"]
    assert len(rejected) == 1
    payload = json.loads(rejected[0].message)
    assert payload == {"actor": "operator", "reason": "tighten AC2"}


def test_approve_handles_stale_revision_without_changing_the_newer_spec(tmp_path, monkeypatch):
    store = _store(tmp_path)
    approve = actions.approve_spec
    newer_state = {}

    def save_newer_then_approve(spec, action_store):
        current = action_store.find_by_id(spec.id)
        assert current is not None
        current.updated_at = datetime(2026, 7, 2, tzinfo=timezone.utc)
        action_store.save(current)
        newer_state.update(current.model_dump())
        approve(spec, action_store)

    monkeypatch.setattr(actions, "approve_spec", save_newer_then_approve)

    out = approve_spec_by_id(store, "s1")

    assert not out.ok
    assert out.message == "spec revision conflict for 's1'; reload and retry"
    assert store.find_by_id("s1").model_dump() == newer_state


def test_request_changes_handles_stale_revision_without_writing_or_changing_the_newer_spec(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    request_changes = actions.request_changes_spec
    newer_state = {}

    def save_newer_then_request_changes(spec, action_store, reason, *, log_manager, actor):
        current = action_store.find_by_id(spec.id)
        assert current is not None
        current.updated_at = datetime(2026, 7, 2, tzinfo=timezone.utc)
        action_store.save(current)
        newer_state.update(current.model_dump())
        request_changes(spec, action_store, reason, log_manager=log_manager, actor=actor)

    monkeypatch.setattr(actions, "request_changes_spec", save_newer_then_request_changes)

    out = request_changes_by_id(store, "s1", "tighten AC2")

    assert not out.ok
    assert out.message == "spec revision conflict for 's1'; reload and retry"
    assert store.find_by_id("s1").model_dump() == newer_state
    assert LogManager(tmp_path / ".mothership" / "logs").read("s1") == []

def test_request_changes_from_linked_worktree_logs_to_main_state(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git_env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": "/tmp", "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["git", "init", "-b", "main"], cwd=main, check=True,
                   capture_output=True, env=git_env)
    (main / "mothership.yaml").write_text("workspace: w\nrepos: {}\n")
    subprocess.run(["git", "add", "-A"], cwd=main, check=True,
                   capture_output=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "c"], cwd=main, check=True,
                   capture_output=True, env=git_env)
    linked = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", str(linked)], cwd=main, check=True,
                   capture_output=True, env=git_env)

    store = _store(linked)
    out = request_changes_by_id(store, "s1", "tighten AC2")

    assert out.ok
    assert store.state_dir == main / ".mothership"
    assert [event.action for event in LogManager(main / ".mothership" / "logs").read("s1")] == ["rejected"]
    assert LogManager(linked / ".mothership" / "logs").read("s1") == []
