from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.message_store import MessageStore
from mship.core.state import StateManager
from mship.core.workitem_store import WorkItemStore


def _client(tmp_path: Path, auth_token=None) -> tuple[TestClient, MessageStore]:
    # Mirror the existing tests/core/test_serve.py::_app construction (create_app
    # requires specs_dir/state_manager/log_manager/workspace_root/workspace_name).
    app = create_app(
        specs_dir=tmp_path / "specs",
        state_manager=StateManager(tmp_path / ".mothership"),
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
        auth_token=auth_token,
    )
    store = MessageStore(tmp_path / ".mothership" / "messages")
    return TestClient(app), store


PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def test_plain_get_threads_unchanged(tmp_path: Path):
    client, store = _client(tmp_path)
    store.create_thread("s", "hi", datetime.now(timezone.utc))
    r = client.get("/threads")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and body[0]["subject"] == "s"  # list shape preserved


def test_wait_returns_changed_when_newer_than_since(tmp_path: Path):
    client, store = _client(tmp_path)
    store.create_thread("s", "hi", datetime.now(timezone.utc))
    r = client.get("/threads", params={"wait": 1, "since": PAST, "timeout": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["timed_out"] is False
    assert body["threads"][0]["subject"] == "s"
    assert "cursor" in body


def test_wait_times_out_with_empty_list(tmp_path: Path):
    client, _ = _client(tmp_path)
    r = client.get("/threads", params={"wait": 1, "timeout": 0.1})  # since defaults to now
    assert r.status_code == 200
    body = r.json()
    assert body["timed_out"] is True
    assert body["threads"] == []


def test_wait_requires_auth(tmp_path: Path):
    client, _ = _client(tmp_path, auth_token="secret")
    r = client.get("/threads", params={"wait": 1, "timeout": 0.1})
    assert r.status_code == 401


def test_wait_invalid_since_returns_422(tmp_path: Path):
    # A malformed ?since= must be a clean 422, not a 500 from an unhandled ValueError.
    client, _ = _client(tmp_path)
    r = client.get("/threads", params={"wait": 1, "since": "notadate", "timeout": 0.1})
    assert r.status_code == 422


def _workitems(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(tmp_path / ".mothership" / "workitems")


def test_summary_stamps_direct_work_item_id(tmp_path: Path):
    # A thread in an item's thread_ids resolves to that item on the list payload.
    client, store = _client(tmp_path)
    now = datetime.now(timezone.utc)
    t = store.create_thread("s", "hi", now)
    items = _workitems(tmp_path)
    wi = items.create("Feature X", "feature", "test-ws", now)
    items.add_thread(wi.id, t.id, now)
    assert client.get("/threads").json()[0]["work_item_id"] == wi.id


def test_summary_stamps_indirect_via_task_slug(tmp_path: Path):
    # A thread linked only by task_slug (no direct thread_ids membership) still resolves.
    client, store = _client(tmp_path)
    now = datetime.now(timezone.utc)
    store.create_thread("s", "hi", now, task_slug="my-task")
    items = _workitems(tmp_path)
    wi = items.create("Feature X", "feature", "test-ws", now)
    items.add_task(wi.id, "my-task", now)
    assert client.get("/threads").json()[0]["work_item_id"] == wi.id


def test_summary_stamps_indirect_via_spec_id(tmp_path: Path):
    client, store = _client(tmp_path)
    now = datetime.now(timezone.utc)
    t = store.create_thread("s", "hi", now)
    store.link_spec(t.id, "spec-42", now)
    items = _workitems(tmp_path)
    wi = items.create("Feature X", "feature", "test-ws", now)
    items.link_spec(wi.id, "spec-42", now)
    assert client.get("/threads").json()[0]["work_item_id"] == wi.id


def test_summary_work_item_id_null_when_unowned(tmp_path: Path):
    client, store = _client(tmp_path)
    store.create_thread("s", "hi", datetime.now(timezone.utc))
    assert client.get("/threads").json()[0]["work_item_id"] is None


def test_summary_resolves_to_single_item_when_also_indirectly_linkable(tmp_path: Path):
    # Direct thread_ids membership is exclusive and outranks the indirect fallback, so the
    # stamped work_item_id is never ambiguous (the merge-watcher routes to the same item).
    client, store = _client(tmp_path)
    now = datetime.now(timezone.utc)
    t = store.create_thread("s", "hi", now, task_slug="my-task")
    items = _workitems(tmp_path)
    owner = items.create("Owner", "feature", "test-ws", now)
    items.add_thread(owner.id, t.id, now)
    other = items.create("Other", "chore", "test-ws", now)
    items.add_task(other.id, "my-task", now)
    assert client.get("/threads").json()[0]["work_item_id"] == owner.id


def test_summary_exposes_awaiting_agent_event(tmp_path: Path):
    # The group attention rollup needs the unhandled-agent-event signal on the list payload.
    client, store = _client(tmp_path)
    now = datetime.now(timezone.utc)
    t = store.create_thread("s", "hi", now)
    assert client.get("/threads").json()[0]["awaiting_agent_event"] is False
    store.append(t.id, "agent", "PR merged", now, kind="event")
    assert client.get("/threads").json()[0]["awaiting_agent_event"] is True


def test_agent_seen_at_exposed_on_list_and_detail(tmp_path: Path):
    # #345: the agent read cursor must be visible to Ground Control on both the thread list
    # (custom summary) and the thread detail (model dump), null when unset.
    client, store = _client(tmp_path)
    now = datetime.now(timezone.utc)
    t = store.create_thread("s", "hi", now)
    assert client.get("/threads").json()[0]["agent_seen_at"] is None
    assert client.get(f"/threads/{t.id}").json()["agent_seen_at"] is None
    store.mark_agent_seen(t.id, now)
    assert client.get("/threads").json()[0]["agent_seen_at"] is not None
    assert client.get(f"/threads/{t.id}").json()["agent_seen_at"] is not None
