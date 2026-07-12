from datetime import datetime, timezone

from mship.core.spec import Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem_gate import GateResult, check_task_gate, log_hotfix, resolve_bound_spec
from mship.core.workitem_store import WorkItemStore


def _now():
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _task(slug="t", work_item_id=None):
    return Task(slug=slug, description="d", phase="dev", created_at=_now(),
                affected_repos=["mothership"], branch="feat/t", work_item_id=work_item_id)


def test_no_work_item_id_is_not_ok(tmp_path):
    result = check_task_gate(_task(work_item_id=None), tmp_path)
    assert isinstance(result, GateResult)
    assert not result.ok
    assert "no WorkItem" in result.reason


def test_missing_work_item_is_not_ok(tmp_path):
    result = check_task_gate(_task(work_item_id="wi-nope"), tmp_path)
    assert not result.ok
    assert "wi-nope" in result.reason


def test_bug_work_item_ok_without_spec(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="fix it", kind="bug", workspace="ws", now=_now())
    result = check_task_gate(_task(work_item_id=wi.id), tmp_path)
    assert result.ok
    assert result.reason is None


def test_feature_work_item_without_approved_spec_is_not_ok(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=_now())
    result = check_task_gate(_task(work_item_id=wi.id), tmp_path)
    assert not result.ok
    assert "approved spec" in result.reason


def test_feature_work_item_with_approved_spec_via_spec_id_is_ok(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    specs = SpecStore(tmp_path / "specs")
    specs.save(Spec(id="spec-1", title="Spec", status="approved",
                    created_at=_now(), updated_at=_now()))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=_now())
    items.link_spec(wi.id, "spec-1", now=_now())
    result = check_task_gate(_task(work_item_id=wi.id), tmp_path)
    assert result.ok


def test_feature_work_item_with_unapproved_spec_via_spec_id_is_not_ok(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    specs = SpecStore(tmp_path / "specs")
    specs.save(Spec(id="spec-1", title="Spec", status="draft",
                    created_at=_now(), updated_at=_now()))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=_now())
    items.link_spec(wi.id, "spec-1", now=_now())
    result = check_task_gate(_task(work_item_id=wi.id), tmp_path)
    assert not result.ok


def test_feature_work_item_with_approved_spec_via_task_slug_fallback_is_ok(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    specs = SpecStore(tmp_path / "specs")
    specs.save(Spec(id="spec-1", title="Spec", status="dispatched",
                    created_at=_now(), updated_at=_now(), task_slug="t"))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=_now())
    # No spec_id link on the WorkItem itself — fallback must scan specs by task_slug.
    result = check_task_gate(_task(slug="t", work_item_id=wi.id), tmp_path)
    assert result.ok


def test_add_task_sets_reverse_link_on_task(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    state = StateManager(tmp_path / ".mothership")
    state.save(WorkspaceState(tasks={"t": Task(
        slug="t", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="feat/t")}))
    wi = items.create(title="add thing", kind="chore", workspace="ws", now=_now())

    items.add_task(wi.id, "t", now=_now(), state=state)

    assert state.load().tasks["t"].work_item_id == wi.id
    assert items.get(wi.id).task_slugs == ["t"]


def test_add_task_without_state_does_not_write_reverse_link(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    state = StateManager(tmp_path / ".mothership")
    state.save(WorkspaceState(tasks={"t": Task(
        slug="t", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="feat/t")}))
    wi = items.create(title="add thing", kind="chore", workspace="ws", now=_now())

    items.add_task(wi.id, "t", now=_now())

    assert state.load().tasks["t"].work_item_id is None


def test_log_hotfix_appends_bypass_log(tmp_path):
    (tmp_path / ".mothership").mkdir()
    log_hotfix(tmp_path, "dev", "some-task")
    log_path = tmp_path / ".mothership" / "bypass-log.jsonl"
    assert log_path.is_file()
    import json
    line = json.loads(log_path.read_text().splitlines()[-1])
    assert line["reason"] == "hotfix"
    assert line["op"] == "dev"
    assert line["branch"] == "some-task"


# ---------------------------------------------------------------------------
# PR review fix: consolidate the "approved or beyond" status set. workitem_gate
# is the single source of truth (APPROVED_STATUSES); workitem_migrate and
# PhaseManager._has_approved_spec must not keep their own hardcoded copies.
# ---------------------------------------------------------------------------

def test_approved_statuses_is_the_expected_set():
    from mship.core.workitem_gate import APPROVED_STATUSES
    assert APPROVED_STATUSES == {"approved", "dispatched", "implemented"}


# ---------------------------------------------------------------------------
# Plan gate (MOS-235): a feature WorkItem must have a valid implementation
# plan before dev/finish — opt-in via `require_plan` (default False, so spawn
# stays plan-free). Validity = plan file resolves AND carries a mship:task
# anchor. bug/chore/question are never plan-gated.
# ---------------------------------------------------------------------------

_PLAN_WITH_TASK = "# Plan\n\n<!-- mship:task id=1 -->\n### Task 1\n<!-- /mship:task -->\n"


def _approved_feature(tmp_path, slug="t"):
    """A feature WorkItem with an approved spec so ONLY the plan is missing."""
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    specs = SpecStore(tmp_path / "specs")
    specs.save(Spec(id="spec-1", title="Spec", status="approved",
                    created_at=_now(), updated_at=_now()))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=_now())
    items.link_spec(wi.id, "spec-1", now=_now())
    return items, wi, _task(slug=slug, work_item_id=wi.id)


def test_feature_without_plan_blocked_when_require_plan(tmp_path):
    _items, _wi, task = _approved_feature(tmp_path)
    res = check_task_gate(task, tmp_path, require_plan=True)
    assert res.ok is False
    assert "plan" in res.reason.lower()


def test_feature_with_convention_plan_passes(tmp_path):
    _items, _wi, task = _approved_feature(tmp_path)
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-07-12-t.md").write_text(_PLAN_WITH_TASK)
    res = check_task_gate(task, tmp_path, require_plan=True)
    assert res.ok is True


def test_feature_with_linked_explicit_plan_passes(tmp_path):
    items, wi, task = _approved_feature(tmp_path)
    custom = tmp_path / "custom" / "myplan.md"
    custom.parent.mkdir(parents=True)
    custom.write_text(_PLAN_WITH_TASK)
    items.link_plan(wi.id, "custom/myplan.md", now=_now())
    res = check_task_gate(task, tmp_path, require_plan=True)
    assert res.ok is True


def test_plan_not_required_by_default(tmp_path):
    # Same env, NO plan doc, default require_plan=False (spawn path) -> ok.
    _items, _wi, task = _approved_feature(tmp_path)
    assert check_task_gate(task, tmp_path).ok is True


def test_empty_plan_file_is_invalid(tmp_path):
    # Plan file exists at the convention path but has no mship:task anchor.
    _items, _wi, task = _approved_feature(tmp_path)
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-07-12-t.md").write_text("# Prose only, no anchor\n")
    res = check_task_gate(task, tmp_path, require_plan=True)
    assert res.ok is False


def test_non_utf8_plan_file_fails_cleanly(tmp_path):
    # A non-UTF-8 / binary plan at the convention path must fail the plan gate
    # cleanly (ok=False), NOT raise UnicodeDecodeError that surfaces as a
    # misleading "corrupt store" error (Greptile).
    _items, _wi, task = _approved_feature(tmp_path)
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-07-12-t.md").write_bytes(b"\xff\xfe binary <!-- mship:task id=1 -->")
    res = check_task_gate(task, tmp_path, require_plan=True)  # must not raise
    assert res.ok is False


def test_bug_never_plan_gated(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="fix it", kind="bug", workspace="ws", now=_now())
    task = _task(work_item_id=wi.id)
    assert check_task_gate(task, tmp_path, require_plan=True).ok is True


def test_workitem_migrate_shares_the_same_approved_statuses_object():
    """workitem_migrate must import (not redefine) workitem_gate's set —
    an identity check, not just an equality check, so a stray local copy
    can't silently drift out of sync."""
    from mship.core import workitem_gate, workitem_migrate
    assert workitem_migrate.APPROVED_STATUSES is workitem_gate.APPROVED_STATUSES


# ---------------------------------------------------------------------------
# ac8 (PR-b): shared resolve_bound_spec(task, workspace_root) -> Spec | None.
# Resolves the spec bound to a task via the WorkItem's spec_id first, then a
# task_slug fallback — the same resolution the WorkItem gate +
# PhaseManager._has_approved_spec use. Never raises (missing/corrupt store -> None).
# ---------------------------------------------------------------------------

def test_resolve_bound_spec_via_workitem_spec_id(tmp_path):
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="F", kind="feature", workspace="ws", now=now)
    SpecStore(tmp_path / "specs").save(Spec(id="s1", title="S", status="approved",
                                            created_at=now, updated_at=now))
    items.link_spec(wi.id, "s1", now=now)
    task = Task(slug="t", description="d", phase="dev", created_at=now,
                affected_repos=["shared"], branch="feat/t", work_item_id=wi.id)
    assert resolve_bound_spec(task, tmp_path).id == "s1"


def _feature_task(tmp_path, now, slug="t"):
    """A task linked to a feature WorkItem with NO spec_id (so the slug fallback,
    which is gated on feature-eligibility, applies)."""
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="F", kind="feature", workspace="ws", now=now)
    return Task(slug=slug, description="d", phase="dev", created_at=now,
                affected_repos=["shared"], branch=f"feat/{slug}", work_item_id=wi.id)


def test_resolve_bound_spec_via_task_slug_fallback(tmp_path):
    # A feature task with no explicit spec_id binds an approved spec by task_slug.
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(id="s2", title="S", status="approved",
                                            created_at=now, updated_at=now, task_slug="t"))
    task = _feature_task(tmp_path, now)
    assert resolve_bound_spec(task, tmp_path).id == "s2"


def test_resolve_bound_spec_none_when_unbound(tmp_path):
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    task = Task(slug="t", description="d", phase="dev", created_at=now,
                affected_repos=["shared"], branch="feat/t")
    assert resolve_bound_spec(task, tmp_path) is None


def test_resolve_bound_spec_bug_task_ignores_slug_collision(tmp_path):
    # Greptile #341 "Slug Collision Binds Unrelated Specs": a bug/chore/hotfix task
    # whose slug coincidentally matches an approved FEATURE spec must NOT bind it —
    # the slug fallback is gated on feature-eligibility. Both a bug WorkItem and a
    # task with no WorkItem must resolve to None here.
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(id="feat-spec", title="S", status="approved",
                                            created_at=now, updated_at=now, task_slug="t"))
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    bug = items.create(title="B", kind="bug", workspace="ws", now=now)
    bug_task = Task(slug="t", description="d", phase="dev", created_at=now,
                    affected_repos=["shared"], branch="feat/t", work_item_id=bug.id)
    assert resolve_bound_spec(bug_task, tmp_path) is None
    hotfix_task = Task(slug="t", description="d", phase="dev", created_at=now,
                       affected_repos=["shared"], branch="feat/t")   # no WorkItem
    assert resolve_bound_spec(hotfix_task, tmp_path) is None


def test_resolve_bound_spec_fallback_skips_non_approved_spec(tmp_path):
    # The feature slug fallback binds only an APPROVED spec — a drafting spec matching
    # the slug is ignored (None), so finish/--require-evidence can't block on a stale
    # checklist and the PR body can't render the wrong one.
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(id="draft-s", title="S", status="drafting",
                                            created_at=now, updated_at=now, task_slug="t"))
    task = _feature_task(tmp_path, now)
    assert resolve_bound_spec(task, tmp_path) is None


def test_resolve_bound_spec_raises_when_multiple_approved_matches(tmp_path):
    # Several APPROVED specs sharing a slug is ambiguous — the resolver RAISES (not
    # None) so finish --require-evidence fails safe rather than opening a PR with the
    # required check silently skipped. (None is reserved for genuinely unbound tasks.)
    import pytest
    from mship.core.workitem_gate import BoundSpecUnresolved
    store = SpecStore(tmp_path / "specs")
    older = datetime(2026, 7, 10, tzinfo=timezone.utc)
    newer = datetime(2026, 7, 12, tzinfo=timezone.utc)
    store.save(Spec(id="one-s", title="one", status="approved",
                    created_at=older, updated_at=older, task_slug="t"))
    store.save(Spec(id="two-s", title="two", status="approved",
                    created_at=older, updated_at=newer, task_slug="t"))
    task = _feature_task(tmp_path, newer)
    with pytest.raises(BoundSpecUnresolved):
        resolve_bound_spec(task, tmp_path)


def test_resolve_bound_spec_raises_when_explicit_link_missing(tmp_path):
    # Greptile #341 "Explicit Link Falls Back": a WorkItem spec_id pointing at a
    # deleted/renamed spec must RAISE (a spec was intended but is gone) — NOT fall
    # back to a slug guess, and NOT silently return None (which would let
    # --require-evidence skip the check). An unrelated approved slug-match exists to
    # prove there's no fallthrough.
    import pytest
    from mship.core.workitem_gate import BoundSpecUnresolved
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="F", kind="feature", workspace="ws", now=now)
    items.link_spec(wi.id, "gone-spec", now=now)          # spec_id set, spec absent
    SpecStore(tmp_path / "specs").save(Spec(id="other-s", title="other", status="approved",
                                            created_at=now, updated_at=now, task_slug="t"))
    task = Task(slug="t", description="d", phase="dev", created_at=now,
                affected_repos=["shared"], branch="feat/t", work_item_id=wi.id)
    with pytest.raises(BoundSpecUnresolved):
        resolve_bound_spec(task, tmp_path)


def test_resolve_bound_spec_explicit_spec_id_bypasses_status_filter(tmp_path):
    # The EXPLICIT WorkItem.spec_id link is authoritative and status-agnostic — a
    # linked spec resolves even while still in review (evidence warnings should
    # surface pre-approval for the linked spec).
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="F", kind="feature", workspace="ws", now=now)
    SpecStore(tmp_path / "specs").save(Spec(id="nr", title="S", status="needs_review",
                                            created_at=now, updated_at=now))
    items.link_spec(wi.id, "nr", now=now)
    task = Task(slug="t", description="d", phase="dev", created_at=now,
                affected_repos=["shared"], branch="feat/t", work_item_id=wi.id)
    assert resolve_bound_spec(task, tmp_path).id == "nr"


def test_resolve_bound_spec_propagates_corrupt_store_error(tmp_path):
    # Greptile #341: a corrupt/unreadable spec store must NOT be silently turned into
    # None (which would make finish --require-evidence skip the required check).
    # It propagates so callers can fail safe.
    (tmp_path / "specs").mkdir(parents=True)
    (tmp_path / "specs" / "2026-07-12-broken.md").write_text("not valid frontmatter, no ---\n")
    # A feature task with no spec_id reaches the slug fallback → SpecStore.list()
    # parses the corrupt file and raises; the resolver must let it propagate.
    task = _feature_task(tmp_path, _now())
    import pytest
    with pytest.raises(Exception):
        resolve_bound_spec(task, tmp_path)


def test_feature_gate_blocks_when_linked_spec_deleted_despite_slug_match(tmp_path):
    # Greptile #341 "Spec Gates Diverge": the feature spec-gate shares
    # resolve_bound_spec's terminal-explicit-link rule, so a feature whose linked
    # spec was deleted does NOT pass the gate just because another approved spec
    # happens to share the task slug (which the old slug fallthrough allowed).
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="F", kind="feature", workspace="ws", now=now)
    items.link_spec(wi.id, "gone-spec", now=now)          # linked spec absent
    SpecStore(tmp_path / "specs").save(Spec(id="other-s", title="other", status="approved",
                                            created_at=now, updated_at=now, task_slug="t"))
    task = Task(slug="t", description="d", phase="dev", created_at=now,
                affected_repos=["shared"], branch="feat/t", work_item_id=wi.id)
    result = check_task_gate(task, tmp_path)
    assert result.ok is False
    assert "approved spec" in (result.reason or "")
