from datetime import datetime, timezone

import pytest

from mship.core.spec import Spec
from mship.core.workitem import WorkItem
from mship.core.view.spec_selection import (
    SpecSelectionError,
    SpecSelector,
    select_by_status,
    select_by_workitem,
    select_default,
    select_spec,
)


def _spec(spec_id, *, status="draft", created):
    return Spec(id=spec_id, title=spec_id, status=status,
                created_at=created, updated_at=created)


def _dt(day):
    return datetime(2026, 7, day, tzinfo=timezone.utc)


def _wi(item_id, *, spec_id=None):
    now = _dt(1)
    return WorkItem(id=item_id, title=item_id, workspace="ws", kind="feature",
                    created_at=now, updated_at=now, spec_id=spec_id)


def test_default_picks_newest_by_created_at_not_mtime():
    specs = [_spec("old", created=_dt(1)), _spec("new", created=_dt(9)),
             _spec("mid", created=_dt(5))]
    assert select_default(specs).id == "new"


def test_default_tie_breaks_on_id():
    specs = [_spec("aaa", created=_dt(3)), _spec("zzz", created=_dt(3))]
    assert select_default(specs).id == "zzz"


def test_default_excludes_archived_unless_all_archived():
    specs = [_spec("live", status="draft", created=_dt(1)),
             _spec("gone", status="archived", created=_dt(9))]
    assert select_default(specs).id == "live"
    only_archived = [_spec("gone", status="archived", created=_dt(9))]
    assert select_default(only_archived).id == "gone"


def test_default_empty_raises():
    with pytest.raises(SpecSelectionError):
        select_default([])


def test_select_by_status_returns_newest_match():
    specs = [_spec("r1", status="needs_review", created=_dt(1)),
             _spec("r2", status="needs_review", created=_dt(7)),
             _spec("a1", status="approved", created=_dt(9))]
    assert select_by_status(specs, "needs_review").id == "r2"


def test_select_by_status_no_match_raises():
    with pytest.raises(SpecSelectionError):
        select_by_status([_spec("a", status="approved", created=_dt(1))], "needs_review")


def test_select_by_workitem_follows_spec_id_link():
    specs = [_spec("s-linked", created=_dt(1)), _spec("s-other", created=_dt(2))]
    items = [_wi("wi-1", spec_id="s-linked")]
    assert select_by_workitem(specs, items, "wi-1").id == "s-linked"


def test_select_by_workitem_unknown_item_raises():
    with pytest.raises(SpecSelectionError):
        select_by_workitem([], [], "wi-missing")


def test_select_by_workitem_item_without_spec_raises():
    with pytest.raises(SpecSelectionError):
        select_by_workitem([], [_wi("wi-1", spec_id=None)], "wi-1")


def test_select_by_workitem_dangling_spec_raises():
    items = [_wi("wi-1", spec_id="ghost")]
    with pytest.raises(SpecSelectionError):
        select_by_workitem([_spec("real", created=_dt(1))], items, "wi-1")


def test_select_spec_precedence_workitem_over_status_over_default():
    specs = [_spec("s-wi", status="draft", created=_dt(1)),
             _spec("s-nr", status="needs_review", created=_dt(2)),
             _spec("s-new", status="approved", created=_dt(9))]
    items = [_wi("wi-1", spec_id="s-wi")]
    assert select_spec(specs, items, SpecSelector(work_item_id="wi-1")).id == "s-wi"
    assert select_spec(specs, items, SpecSelector(status="needs_review")).id == "s-nr"
    assert select_spec(specs, items, SpecSelector()).id == "s-new"
