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
