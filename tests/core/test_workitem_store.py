from datetime import datetime, timezone

import pytest

from mship.core.workitem_store import WorkItemStore


def _now():
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def test_create_get_roundtrip(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    wi = store.create(title="Make capture conversational", kind="feature",
                      workspace="mothership", now=_now())
    assert wi.id
    assert store.get(wi.id) == wi


def test_list_sorted_by_updated_desc(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    a = store.create(title="a", kind="bug", workspace="ws",
                     now=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc))
    b = store.create(title="b", kind="bug", workspace="ws",
                     now=datetime(2026, 6, 30, 11, 0, tzinfo=timezone.utc))
    assert [w.id for w in store.list()] == [b.id, a.id]


def test_unsafe_id_rejected(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    with pytest.raises(ValueError):
        store.get("../escape")


def test_link_helpers(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    wi = store.create(title="t", kind="feature", workspace="ws", now=_now())
    store.link_spec(wi.id, "spec-1", now=_now())
    store.add_task(wi.id, "task-a", now=_now())
    store.add_task(wi.id, "task-a", now=_now())  # idempotent
    store.add_thread(wi.id, "thread-x", now=_now())
    store.set_phase_override(wi.id, "in_flight", now=_now())
    got = store.get(wi.id)
    assert got.spec_id == "spec-1"
    assert got.task_slugs == ["task-a"]
    assert got.thread_ids == ["thread-x"]
    assert got.phase_override == "in_flight"


def test_link_missing_item_raises(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    with pytest.raises(KeyError):
        store.link_spec("nope", "spec-1")


def test_set_unattended_toggles(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    wi = store.create(title="t", kind="feature", workspace="ws",
                      now=datetime(2026, 7, 8, tzinfo=timezone.utc))
    store.set_unattended(wi.id, True, now=datetime(2026, 7, 8, 1, tzinfo=timezone.utc))
    assert store.get(wi.id).unattended is True
    store.set_unattended(wi.id, False, now=datetime(2026, 7, 8, 2, tzinfo=timezone.utc))
    assert store.get(wi.id).unattended is False


def test_redundant_add_task_does_not_bump_updated_at(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    wi = store.create(title="t", kind="feature", workspace="ws", now=_now())
    store.add_task(wi.id, "task-a", now=_now())
    later = datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc)
    store.add_task(wi.id, "task-a", now=later)  # already present: no-op
    got = store.get(wi.id)
    assert got.task_slugs == ["task-a"]
    assert got.updated_at == _now()


def test_redundant_add_thread_does_not_bump_updated_at(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    wi = store.create(title="t", kind="feature", workspace="ws", now=_now())
    store.add_thread(wi.id, "thread-x", now=_now())
    later = datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc)
    store.add_thread(wi.id, "thread-x", now=later)  # already present: no-op
    got = store.get(wi.id)
    assert got.thread_ids == ["thread-x"]
    assert got.updated_at == _now()


def test_archive_sets_flag_and_persists(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    wi = store.create(title="t", kind="feature", workspace="ws", now=_now())
    store.archive(wi.id, now=datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc))
    # reload via a fresh store instance to prove it was persisted, not just mutated in memory
    fresh = WorkItemStore(tmp_path / "workitems")
    got = fresh.get(wi.id)
    assert got.archived is True
    assert got.updated_at == datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc)


def test_unarchive_clears_flag(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    wi = store.create(title="t", kind="feature", workspace="ws", now=_now())
    store.archive(wi.id, now=_now())
    store.unarchive(wi.id, now=datetime(2026, 6, 30, 14, 0, tzinfo=timezone.utc))
    fresh = WorkItemStore(tmp_path / "workitems")
    got = fresh.get(wi.id)
    assert got.archived is False
    assert got.updated_at == datetime(2026, 6, 30, 14, 0, tzinfo=timezone.utc)


def test_list_excludes_archived_by_default(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    a = store.create(title="a", kind="bug", workspace="ws",
                     now=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc))
    b = store.create(title="b", kind="bug", workspace="ws",
                     now=datetime(2026, 6, 30, 11, 0, tzinfo=timezone.utc))
    store.archive(b.id, now=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc))
    assert [w.id for w in store.list()] == [a.id]


def test_list_include_archived_returns_all(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    a = store.create(title="a", kind="bug", workspace="ws",
                     now=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc))
    b = store.create(title="b", kind="bug", workspace="ws",
                     now=datetime(2026, 6, 30, 11, 0, tzinfo=timezone.utc))
    store.archive(b.id, now=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc))
    assert {w.id for w in store.list(include_archived=True)} == {a.id, b.id}


def test_get_loads_legacy_workitem_json_without_archived_field(tmp_path):
    workitems_dir = tmp_path / "workitems"
    workitems_dir.mkdir(parents=True)
    legacy_json = (
        '{"id": "wi-legacy", "title": "pre-archive item", "workspace": "ws", '
        '"kind": "feature", "created_at": "2026-06-30T10:00:00Z", '
        '"updated_at": "2026-06-30T10:00:00Z", "spec_id": null, '
        '"task_slugs": [], "thread_ids": [], "external_links": [], '
        '"phase_override": null, "unattended": false}'
    )
    (workitems_dir / "wi-legacy.json").write_text(legacy_json)
    store = WorkItemStore(workitems_dir)
    got = store.get("wi-legacy")
    assert got.archived is False


def test_archive_missing_item_raises(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    with pytest.raises(KeyError):
        store.archive("nope")


def test_unarchive_missing_item_raises(tmp_path):
    store = WorkItemStore(tmp_path / "workitems")
    with pytest.raises(KeyError):
        store.unarchive("nope")
