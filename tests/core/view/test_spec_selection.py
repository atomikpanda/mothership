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


# --- Task 2: canonical read is branch/worktree independent (AC1) ---

from mship.core.spec_store import SpecStore
from mship.core.view.spec_selection import load_canonical_specs


def _seed(store_dir, spec_id, *, created):
    store = SpecStore(store_dir)
    return store.save(Spec(id=spec_id, title=spec_id, status="draft",
                           created_at=created, updated_at=created,
                           body=f"Body of {spec_id}\n"))


def test_load_canonical_specs_reads_only_the_workspace_store(tmp_path):
    # Canonical store at <root>/specs.
    _seed(tmp_path / "specs", "canonical-one", created=_dt(3))
    # A task worktree with its OWN legacy specs dir that must be ignored (AC1).
    wt_specs = tmp_path / "wt-feature" / "docs" / "superpowers" / "specs"
    wt_specs.mkdir(parents=True)
    (wt_specs / "worktree-only.md").write_text("# worktree only\n")

    specs = load_canonical_specs(tmp_path / "specs")
    assert [s.id for s in specs] == ["canonical-one"]


def test_load_canonical_specs_skips_unparseable(tmp_path):
    _seed(tmp_path / "specs", "good", created=_dt(2))
    (tmp_path / "specs" / "raw-no-frontmatter.md").write_text("# no frontmatter\n")
    assert [s.id for s in load_canonical_specs(tmp_path / "specs")] == ["good"]


def test_load_canonical_specs_missing_dir_is_empty(tmp_path):
    assert load_canonical_specs(tmp_path / "specs") == []


def test_select_default_over_canonical_store_round_trip(tmp_path):
    _seed(tmp_path / "specs", "older", created=_dt(1))
    _seed(tmp_path / "specs", "newest", created=_dt(8))
    specs = load_canonical_specs(tmp_path / "specs")
    assert select_default(specs).id == "newest"
