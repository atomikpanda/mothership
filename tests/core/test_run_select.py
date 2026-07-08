from datetime import datetime, timezone
from mship.core.run_select import select_runnable, Candidate

NOW = datetime(2026, 7, 8, tzinfo=timezone.utc)

def _wi(id, unattended=True, phase="ready", spec="s", created=NOW):
    from mship.core.workitem import WorkItem
    return WorkItem(id=id, title=id, workspace="ws", kind="feature",
                    created_at=created, updated_at=created, spec_id=spec,
                    unattended=unattended, phase_override=phase)

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
