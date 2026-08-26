from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.message_store import MessageStore
from mship.core.state import StateManager
from mship.core.workitem_store import WorkItemStore
from mship.core.spec import Spec
from mship.core.spec_store import SpecStore, serialize_spec


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


def test_wait_applies_inbox_and_search_filters_without_changing_cursor(tmp_path: Path):
    client, store = _client(tmp_path)
    updated_at = datetime.now(timezone.utc) - timedelta(days=7, seconds=1)
    thread = store.create_thread("Needle old thread", "body", updated_at)
    store.append(thread.id, "agent", "resolved", updated_at)
    since = (updated_at - timedelta(seconds=1)).isoformat()

    response = client.get("/threads", params={
        "wait": 1, "since": since, "timeout": 0.1, "inbox": "archived", "q": "needle",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is False
    assert [summary["id"] for summary in body["threads"]] == [thread.id]
    assert body["cursor"] == updated_at.isoformat()



@pytest.mark.parametrize(
    ("action", "expected_state", "initial_age", "setup_action"),
    [
        ("archive", "archived", timedelta(minutes=1), None),
        ("restore", "active", timedelta(days=8), None),
        ("pin", "active", timedelta(minutes=1), None),
        ("unpin", "active", timedelta(minutes=1), "pin"),
    ],
)
def test_each_inbox_action_wakes_wait_once_without_changing_content_timestamp(
    tmp_path: Path,
    action: str,
    expected_state: str,
    initial_age: timedelta,
    setup_action: str | None,
):
    client, store = _client(tmp_path)
    created_at = datetime.now(timezone.utc) - initial_age
    thread = store.create_thread("inbox mutation", "body", created_at)
    if action in {"archive", "restore"}:
        store.append(thread.id, "agent", "resolved", created_at)
    if action == "restore":
        assert client.get("/threads", params={"inbox": "archived"}).json()[0]["id"] == thread.id
    since = created_at
    if setup_action is not None:
        setup_response = client.post(
            f"/threads/{thread.id}/inbox/{setup_action}",
            json={"mutation_id": f"setup-{setup_action}"},
        )
        assert setup_response.status_code == 200
        since = store.get(thread.id).inbox.last_mutated_at
        assert since is not None

    action_response = client.post(
        f"/threads/{thread.id}/inbox/{action}", json={"mutation_id": f"device-{action}"},
    )
    assert action_response.status_code == 200
    assert action_response.json()["inbox_state"] == expected_state
    assert store.get(thread.id).updated_at == created_at

    woke = client.get("/threads", params={"wait": 1, "since": since.isoformat(), "timeout": 0})
    assert woke.status_code == 200
    assert woke.json()["timed_out"] is False
    assert [summary["id"] for summary in woke.json()["threads"]] == [thread.id]
    cursor = woke.json()["cursor"]

    retry = client.post(
        f"/threads/{thread.id}/inbox/{action}", json={"mutation_id": f"device-{action}"},
    )
    assert retry.status_code == 200
    no_second_change = client.get("/threads", params={"wait": 1, "since": cursor, "timeout": 0})
    assert no_second_change.status_code == 200
    assert no_second_change.json()["timed_out"] is True
    assert no_second_change.json()["threads"] == []
    assert no_second_change.json()["cursor"] == cursor


def test_unpin_noop_does_not_wake_wait(tmp_path: Path):
    client, store = _client(tmp_path)
    created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    thread = store.create_thread("inbox mutation", "body", created_at)

    response = client.post(
        f"/threads/{thread.id}/inbox/unpin", json={"mutation_id": "device-unpin"},
    )
    assert response.status_code == 200
    saved = store.get(thread.id)
    assert saved.inbox.mutation_ids == {"device-unpin": "unpin"}
    assert saved.inbox.last_mutated_at is None

    wait = client.get(
        "/threads",
        params={"wait": 1, "since": created_at.isoformat(), "timeout": 0},
    )
    assert wait.status_code == 200
    assert wait.json()["timed_out"] is True
    assert wait.json()["threads"] == []


@pytest.mark.parametrize(
    ("initial_age", "action", "inbox"),
    [
        (timedelta(minutes=1), "archive", "active"),
        (timedelta(days=8), "restore", "archived"),
    ],
)
def test_wait_reports_removed_ids_when_an_inbox_mutation_leaves_the_filter(
    tmp_path: Path, initial_age: timedelta, action: str, inbox: str,
):
    client, store = _client(tmp_path)
    updated_at = datetime.now(timezone.utc) - initial_age
    thread = store.create_thread("filter transition", "body", updated_at)
    store.append(thread.id, "agent", "resolved", updated_at)
    client.post(f"/threads/{thread.id}/inbox/{action}", json={"mutation_id": f"device-{action}"})

    response = client.get("/threads", params={
        "wait": 1, "since": updated_at.isoformat(), "timeout": 0, "inbox": inbox,
    })

    assert response.status_code == 200
    assert response.json()["timed_out"] is False
    assert response.json()["threads"] == []
    assert response.json()["removed_ids"] == [thread.id]

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


def test_duplicate_spec_ids_keep_only_their_linked_threads_active(tmp_path: Path):
    """A duplicate spec id must not arbitrarily make its linked thread terminal."""
    client, messages = _client(tmp_path)
    now = datetime.now(timezone.utc)
    items = _workitems(tmp_path)
    ambiguous_thread = messages.create_thread("ambiguous", "body", now)
    messages.link_spec(ambiguous_thread.id, "duplicate-spec", now)
    ambiguous_item = items.create("Ambiguous", "feature", "test-ws", now)
    items.link_spec(ambiguous_item.id, "duplicate-spec", now)

    healthy_thread = messages.create_thread("healthy", "body", now)
    messages.link_spec(healthy_thread.id, "healthy-spec", now)
    healthy_item = items.create("Healthy", "feature", "test-ws", now)
    items.link_spec(healthy_item.id, "healthy-spec", now)

    specs = SpecStore(tmp_path / "specs")
    specs.save(Spec(
        id="duplicate-spec", title="draft", status="draft",
        created_at=now, updated_at=now,
    ))
    duplicate = Spec(
        id="duplicate-spec", title="implemented copy", status="implemented",
        created_at=now, updated_at=now,
    )
    (tmp_path / "specs" / "z-duplicate.md").write_text(serialize_spec(duplicate))
    specs.save(Spec(
        id="healthy-spec", title="implemented", status="implemented",
        created_at=now, updated_at=now,
    ))
    messages.append(ambiguous_thread.id, "agent", "resolved", now)
    messages.append(healthy_thread.id, "agent", "resolved", now)

    summaries = {summary["id"]: summary for summary in client.get("/threads").json()}
    assert summaries[ambiguous_thread.id]["inbox_state"] == "active"
    assert summaries[healthy_thread.id]["inbox_state"] == "archived"
