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


# --- MOS-228 T3: GET /items archived filter; GET /items/{id} stays unfiltered ---

def test_list_items_excludes_archived_by_default(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    visible = items.create(title="Visible", kind="feature", workspace="testws", now=_now())
    hidden = items.create(title="Hidden", kind="feature", workspace="testws", now=_now())
    items.archive(hidden.id, now=_now())
    client = _app(tmp_path)

    listed = client.get("/items").json()
    assert [i["id"] for i in listed] == [visible.id]


def test_list_items_include_archived_query_flag_shows_archived(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    visible = items.create(title="Visible", kind="feature", workspace="testws", now=_now())
    hidden = items.create(title="Hidden", kind="feature", workspace="testws", now=_now())
    items.archive(hidden.id, now=_now())
    client = _app(tmp_path)

    listed = client.get("/items", params={"include_archived": True}).json()
    assert {i["id"] for i in listed} == {visible.id, hidden.id}


def test_get_item_by_id_returns_archived_item_unfiltered(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Archived thing", kind="feature", workspace="testws", now=_now())
    items.archive(wi.id, now=_now())
    client = _app(tmp_path)

    # A direct fetch by id is not subject to the archived filter, unlike the list.
    assert client.get("/items").json() == []
    resp = client.get(f"/items/{wi.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == wi.id


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


# --- gc32 ac3: POST /items/{id}/phase (Mark done / Reopen) ---

def test_post_item_phase_sets_override(tmp_path):
    """A config with no hooks configured for this phase still applies the
    override cleanly — config presence is what matters, not whether a hook
    happens to be registered for this particular phase name."""
    from mship.core.config import WorkspaceConfig

    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Stuck item", kind="feature", workspace="testws", now=_now())
    config = WorkspaceConfig(workspace="testws", repos={}, lifecycle_hooks=[])
    client = _app_with_config(tmp_path, config)

    resp = client.post(f"/items/{wi.id}/phase", json={"phase": "done"})
    assert resp.status_code == 200
    assert resp.json() == {"id": wi.id, "phase_override": "done"}

    # Persisted, and reflected in the derived summary.
    assert items.get(wi.id).phase_override == "done"
    assert client.get(f"/items/{wi.id}").json()["phase"] == "done"


def test_post_item_phase_clears_override_when_null(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Reopen me", kind="feature", workspace="testws", now=_now())
    items.set_phase_override(wi.id, "done", now=_now())
    client = _app(tmp_path)

    resp = client.post(f"/items/{wi.id}/phase", json={"phase": None})
    assert resp.status_code == 200
    assert resp.json() == {"id": wi.id, "phase_override": None}

    # Override cleared -> item returns to its derived phase (inbox, no children).
    assert items.get(wi.id).phase_override is None
    assert client.get(f"/items/{wi.id}").json()["phase"] == "inbox"


def test_post_item_phase_404_for_unknown_item(tmp_path):
    client = _app(tmp_path)
    assert client.post("/items/nope/phase", json={"phase": "done"}).status_code == 404


def test_post_item_phase_rejects_invalid_phase(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Item", kind="feature", workspace="testws", now=_now())
    client = _app(tmp_path)
    assert client.post(f"/items/{wi.id}/phase", json={"phase": "bogus"}).status_code == 422


# --- Lifecycle hooks (MOS-220, spec mship-lifecycle-hooks): `workitem.phase.<phase>` ---


def _app_with_config(tmp_path, config):
    specs_dir = tmp_path / "specs"
    SpecStore(specs_dir)  # ensure dir resolvable
    state_manager = StateManager(tmp_path / ".mothership")
    app = create_app(
        specs_dir=specs_dir, state_manager=state_manager, log_manager=None,
        workspace_root=tmp_path, workspace_name="testws", config=config,
    )
    return TestClient(app)


def test_post_item_phase_fires_workitem_phase_hook(tmp_path):
    from mship.core.config import HookConfig, WorkspaceConfig

    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Stuck item", kind="feature", workspace="testws", now=_now())

    marker = tmp_path / "hook-fired.txt"
    config = WorkspaceConfig(
        workspace="testws", repos={},
        lifecycle_hooks=[HookConfig(on="workitem.phase.done", run=f"touch {marker}")],
    )
    client = _app_with_config(tmp_path, config)

    resp = client.post(f"/items/{wi.id}/phase", json={"phase": "done"})
    assert resp.status_code == 200
    assert marker.exists()
    assert items.get(wi.id).phase_override == "done"


def test_post_item_phase_required_hook_failure_returns_422_and_blocks_override(tmp_path):
    from mship.core.config import HookConfig, WorkspaceConfig

    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Stuck item", kind="feature", workspace="testws", now=_now())

    config = WorkspaceConfig(
        workspace="testws", repos={},
        lifecycle_hooks=[HookConfig(on="workitem.phase.done", run="false", required=True)],
    )
    client = _app_with_config(tmp_path, config)

    resp = client.post(f"/items/{wi.id}/phase", json={"phase": "done"})
    assert resp.status_code == 422
    assert items.get(wi.id).phase_override is None


def test_post_item_phase_non_required_hook_failure_still_sets_override(tmp_path):
    from mship.core.config import HookConfig, WorkspaceConfig

    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Stuck item", kind="feature", workspace="testws", now=_now())

    config = WorkspaceConfig(
        workspace="testws", repos={},
        lifecycle_hooks=[HookConfig(on="workitem.phase.done", run="false")],
    )
    client = _app_with_config(tmp_path, config)

    resp = client.post(f"/items/{wi.id}/phase", json={"phase": "done"})
    assert resp.status_code == 200
    assert items.get(wi.id).phase_override == "done"


def test_post_item_phase_hook_fires_iff_config_present(tmp_path):
    """MOS-220 Greptile fix ("Configless Server Bypasses Hooks", #312): a
    configless serve instance cannot evaluate lifecycle hooks at all, so it
    must refuse the phase change (503) rather than silently applying it —
    the earlier behavior (apply anyway, just skip the hook) was itself the
    bypass this test now guards against. Same hook definition, same
    WorkItemStore dir; the only variable between the two calls below is
    whether `config` is wired into `create_app` at all."""
    from mship.core.config import HookConfig, WorkspaceConfig

    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi_no_config = items.create(title="No config", kind="feature", workspace="testws", now=_now())
    wi_with_config = items.create(title="With config", kind="feature", workspace="testws", now=_now())

    marker = tmp_path / "hook-fired.txt"
    hooks = [HookConfig(on="workitem.phase.done", run=f"touch {marker}")]

    # No config wired into create_app at all -> nothing to evaluate hooks
    # against, so the endpoint must fail closed: refuse the mutation rather
    # than bypassing whatever hook policy would otherwise apply.
    client_no_config = _app(tmp_path)
    resp = client_no_config.post(f"/items/{wi_no_config.id}/phase", json={"phase": "done"})
    assert resp.status_code == 503
    assert not marker.exists()
    assert items.get(wi_no_config.id).phase_override is None

    # Same hook definition, config now wired in -> it must fire, and the
    # override applies normally.
    config = WorkspaceConfig(workspace="testws", repos={}, lifecycle_hooks=hooks)
    client_with_config = _app_with_config(tmp_path, config)
    resp = client_with_config.post(f"/items/{wi_with_config.id}/phase", json={"phase": "done"})
    assert resp.status_code == 200
    assert marker.exists()
    assert items.get(wi_with_config.id).phase_override == "done"


def test_post_item_phase_hook_does_not_hold_item_msg_lock(tmp_path):
    """MOS-220 Greptile fix ("Hook Holds Lock"): a slow workitem.phase.* hook
    must not stall unrelated item/message operations that share
    `_item_msg_lock` (POST /items/{id}/messages, /unattended, /phase itself).
    Proven with a hook that sleeps: a concurrent POST /items/{other_id}/
    messages against the SAME client must complete quickly, not wait out the
    hook's sleep — which it would if the endpoint still ran the hook while
    holding the lock."""
    import threading
    import time

    from mship.core.config import HookConfig, WorkspaceConfig

    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    slow_item = items.create(title="Slow phase", kind="feature", workspace="testws", now=_now())
    other_item = items.create(title="Unrelated", kind="feature", workspace="testws", now=_now())

    config = WorkspaceConfig(
        workspace="testws", repos={},
        lifecycle_hooks=[HookConfig(on="workitem.phase.done", run="sleep 1")],
    )
    client = _app_with_config(tmp_path, config)

    phase_result: dict = {}

    def _fire_slow_phase():
        t0 = time.monotonic()
        resp = client.post(f"/items/{slow_item.id}/phase", json={"phase": "done"})
        phase_result["status"] = resp.status_code
        phase_result["elapsed"] = time.monotonic() - t0

    thread = threading.Thread(target=_fire_slow_phase)
    thread.start()
    time.sleep(0.2)  # let the phase request start running its (slow) hook

    t0 = time.monotonic()
    msg_resp = client.post(f"/items/{other_item.id}/messages", json={"text": "hi"})
    elapsed = time.monotonic() - t0

    thread.join(timeout=5)

    assert msg_resp.status_code == 200
    assert elapsed < 0.5, f"messages POST took {elapsed}s — looks like it waited on the phase hook"
    assert phase_result["status"] == 200
    assert phase_result["elapsed"] >= 1.0
    assert items.get(slow_item.id).phase_override == "done"


def test_get_spec_includes_resolved_work_item_kind(tmp_path):
    # The Queue review cards show the WorkItem kind; get_spec resolves it from the linked item.
    from mship.core.spec import Spec
    from mship.core.spec_store import SpecStore
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Feat", kind="feature", workspace="testws", now=_now())
    store = SpecStore(tmp_path / "specs")
    store.save(Spec(id="linked", title="Linked", status="needs_review",
                    created_at=_now(), updated_at=_now(), work_item_id=wi.id))
    store.save(Spec(id="unlinked", title="Unlinked", status="needs_review",
                    created_at=_now(), updated_at=_now()))
    client = _app(tmp_path)
    assert client.get("/specs/linked").json()["work_item_kind"] == "feature"
    assert client.get("/specs/unlinked").json()["work_item_kind"] is None
