# tests/core/test_serve_items.py
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from mship.core.message_store import MessageStore
from mship.core.serve import create_app
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager
from mship.core.workitem_store import WorkItemStore


def _now():
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _app(tmp_path):
    specs_dir = tmp_path / "specs"
    SpecStore(specs_dir)  # ensure dir resolvable
    state_manager = StateManager(tmp_path / ".mothership")
    app = create_app(specs_dir=specs_dir, state_manager=state_manager, log_manager=None,
                     workspace_root=tmp_path, workspace_name="testws")
    return TestClient(app)


def test_list_items_empty(tmp_path):
    client = _app(tmp_path)
    assert client.get("/items").json() == []


def test_list_and_get_item_with_derived_phase(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Captured idea", kind="question", workspace="testws", now=_now())
    client = _app(tmp_path)

    listed = client.get("/items").json()
    assert len(listed) == 1
    assert listed[0]["id"] == wi.id
    assert listed[0]["phase"] == "inbox"
    assert listed[0]["attention"]["blocked"] is False

    got = client.get(f"/items/{wi.id}").json()
    assert got["id"] == wi.id and got["phase"] == "inbox"

    assert client.get("/items/nope").status_code == 404


def test_post_item_message_creates_and_links_thread_when_none(tmp_path):
    """An in-flight item created from a spec/task has no thread; steering it must
    lazily create+link one (not silently no-op) so the message lands somewhere."""
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Ship the parser", kind="feature", workspace="testws", now=_now())
    assert wi.thread_ids == []
    client = _app(tmp_path)

    resp = client.post(f"/items/{wi.id}/messages", json={"text": "focus on the edge cases"})
    assert resp.status_code == 200
    thread = resp.json()
    assert [m["text"] for m in thread["messages"]] == ["focus on the edge cases"]
    assert thread["subject"] == "Ship the parser"

    # The work item is now linked to the new thread, so the console can find it.
    relinked = items.get(wi.id)
    assert relinked.thread_ids == [thread["id"]]
    assert client.get(f"/items/{wi.id}").json()["thread_ids"] == [thread["id"]]


def test_post_item_message_appends_to_existing_thread(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    msgs = MessageStore(tmp_path / ".mothership" / "messages")
    wi = items.create(title="Ship the parser", kind="feature", workspace="testws", now=_now())
    thread = msgs.create_thread(subject="Ship the parser", text="first", now=_now())
    items.add_thread(wi.id, thread.id, now=_now())
    client = _app(tmp_path)

    resp = client.post(f"/items/{wi.id}/messages", json={"text": "second"})
    assert resp.status_code == 200
    assert [m["text"] for m in resp.json()["messages"]] == ["first", "second"]
    # No duplicate thread was created.
    assert items.get(wi.id).thread_ids == [thread.id]


def test_post_item_message_404_for_unknown_item(tmp_path):
    client = _app(tmp_path)
    assert client.post("/items/nope/messages", json={"text": "hi"}).status_code == 404
