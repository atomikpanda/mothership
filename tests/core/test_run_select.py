from datetime import datetime, timezone
from mship.core.run_select import select_runnable, Candidate

NOW = datetime(2026, 7, 8, tzinfo=timezone.utc)

def _wi(id, unattended=True, phase="ready", spec="s", created=NOW, tasks=()):
    from mship.core.workitem import WorkItem
    return WorkItem(id=id, title=id, workspace="ws", kind="feature",
                    created_at=created, updated_at=created, spec_id=spec,
                    unattended=unattended, phase_override=phase, task_slugs=list(tasks))

def _spec(status, id="s"):
    from mship.core.spec import Spec
    return Spec(id=id, title=id, status=status, created_at=NOW, updated_at=NOW)

def _task(slug, *, finished=False):
    from mship.core.state import Task
    return Task(slug=slug, description="d", phase="dev", created_at=NOW,
                affected_repos=["mothership"], branch="b",
                finished_at=NOW if finished else None)

def test_selects_only_eligible_oldest_first():
    items = [
        _wi("wi-new", created=datetime(2026,7,8,2,tzinfo=timezone.utc)),
        _wi("wi-old", created=datetime(2026,7,8,1,tzinfo=timezone.utc)),
        _wi("wi-notflagged", unattended=False),
        _wi("wi-notready", phase="shaping"),
        _wi("wi-nospec", spec=None),
    ]
    spec_approved = {"s": True}                      # spec id -> is approved
    claimed = set()                                  # item ids currently claimed
    out = select_runnable(items, spec_approved, claimed)
    assert [c.item.id for c in out] == ["wi-old", "wi-new"]  # eligible, oldest first

def test_excludes_claimed_and_unapproved_spec():
    items = [_wi("wi-1"), _wi("wi-2", spec="unapproved")]
    assert [c.item.id for c in select_runnable(items, {"s": True, "unapproved": False}, {"wi-1"})] == []


def test_excludes_blocked_items():
    # A bailed item (its linked task carries a blocked_reason) must NOT be re-picked
    # forever — selection excludes ids in the `blocked` set. #unattended-runner FIX#1
    items = [_wi("wi-1"), _wi("wi-2")]
    out = select_runnable(items, {"s": True}, claimed=set(), blocked={"wi-1"})
    assert [c.item.id for c in out] == ["wi-2"]


# --- Finding 3: the selector uses the DERIVED phase, not only phase_override ---

def test_selects_item_with_no_override_but_derived_ready():
    # Reopen clears the override; an approved-spec item then derives to `ready` via
    # compute_phase (no tasks). The selector must see that derived `ready` — reading
    # only phase_override would see `inbox` and skip the item forever.
    it = _wi("wi-derived", phase=None)          # no override
    out = select_runnable(
        [it], {"s": True}, claimed=set(),
        specs_by_id={"s": _spec("approved")}, tasks_by_slug={},
    )
    assert [c.item.id for c in out] == ["wi-derived"]


def test_derived_in_flight_not_selected_even_when_spec_approved():
    # An approved spec with a still-running task derives to in_flight, not ready. The
    # spec_approved gate passes, so ONLY the derived-phase check keeps it out — proving
    # the selector consults the derived phase and not just spec approval.
    it = _wi("wi-running", phase=None, tasks=["t1"])
    out = select_runnable(
        [it], {"s": True}, claimed=set(),
        specs_by_id={"s": _spec("approved")}, tasks_by_slug={"t1": _task("t1")},
    )
    assert out == []


def test_no_override_no_spec_info_is_not_ready():
    # No override and nothing to derive from -> inbox -> not selectable (safe default).
    it = _wi("wi-blank", phase=None)
    assert select_runnable([it], {"s": True}, claimed=set()) == []


# --- MOS-240: run-next selection is unchanged under the collapsed status set ---

def test_run_select_parity_across_collapsed_statuses():
    """Regression (MOS-240): only an `approved` spec derives `ready` and is
    selected; every other collapsed status (draft/needs_review derive `shaping`,
    dispatched `in_flight`, implemented/archived `done`) stays unselected — exactly
    as the pre-collapse vocabulary did. Selection is invariant to the rename."""
    # `approved` -> ready -> selected (spec_approved gate also passes).
    approved = _wi("wi-approved", phase=None)
    assert [c.item.id for c in select_runnable(
        [approved], {"s": True}, claimed=set(),
        specs_by_id={"s": _spec("approved")}, tasks_by_slug={},
    )] == ["wi-approved"]

    # Every non-approved collapsed status derives a non-ready phase -> not selected.
    for status in ("draft", "needs_review", "dispatched", "implemented", "archived"):
        it = _wi(f"wi-{status}", phase=None)
        out = select_runnable(
            [it], {"s": True}, claimed=set(),
            specs_by_id={"s": _spec(status)}, tasks_by_slug={},
        )
        assert out == [], f"{status!r} must not be run-selected"
