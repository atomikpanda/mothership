# tests/core/test_serve_items.py
from datetime import datetime, timezone

from fastapi.testclient import TestClient

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
