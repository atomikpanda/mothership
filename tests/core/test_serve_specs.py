from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.spec import Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(
        specs_dir=tmp_path / "specs",
        state_manager=StateManager(tmp_path / ".mothership"),
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
    ))


def _seed(tmp_path: Path) -> tuple[Spec, Spec]:
    now = datetime.now(timezone.utc)
    active = Spec(
        id="active", title="Needle design", status="draft",
        created_at=now, updated_at=now,
    )
    archived = Spec(
        id="archived", title="Completed design", status="implemented",
        created_at=now - timedelta(days=8), updated_at=now - timedelta(days=8),
        body="implementation content",
    )
    store = SpecStore(tmp_path / "specs")
    store.save(active)
    store.save(archived)
    return active, archived


def test_specs_filter_search_and_compatibility_default(tmp_path: Path):
    active, archived = _seed(tmp_path)
    client = _client(tmp_path)

    all_specs = client.get("/specs")
    assert all_specs.status_code == 200
    assert {spec["id"] for spec in all_specs.json()} == {active.id, archived.id}
    assert {spec["inbox_state"] for spec in all_specs.json()} == {"active", "archived"}
    assert next(spec for spec in all_specs.json() if spec["id"] == archived.id)["archive_reason"] == "implemented"
    assert client.get("/specs", params={"inbox": "active", "q": "needle"}).json()[0]["id"] == active.id
    assert client.get("/specs", params={"inbox": "archived"}).json()[0]["id"] == archived.id
    assert client.get("/specs", params={"inbox": "unknown"}).status_code == 422


def test_spec_inbox_mutations_are_ordered_idempotent_and_non_destructive(tmp_path: Path):
    active, _ = _seed(tmp_path)
    client = _client(tmp_path)

    first = client.post(f"/specs/{active.id}/inbox/archive", json={"mutation_id": "phone-archive-1"})
    retry = client.post(f"/specs/{active.id}/inbox/archive", json={"mutation_id": "phone-archive-1"})
    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    assert first.json()["inbox_state"] == "archived"
    assert client.post(f"/specs/{active.id}/inbox/pin", json={"mutation_id": "desktop-pin-1"}).json()["inbox_state"] == "active"
    assert client.post(f"/specs/{active.id}/inbox/unpin", json={"mutation_id": "phone-unpin-1"}).json()["inbox_state"] == "archived"
    assert client.post(f"/specs/{active.id}/inbox/restore", json={"mutation_id": "desktop-restore-1"}).json()["inbox_state"] == "active"
    assert SpecStore(tmp_path / "specs").find_by_id(active.id) is not None
    assert client.post(f"/specs/{active.id}/inbox/archive", json={"mutation_id": " "}).status_code == 422
    assert client.post("/specs/nope/inbox/archive", json={"mutation_id": "missing-1"}).status_code == 404
    assert client.post(f"/specs/{active.id}/inbox/nope", json={"mutation_id": "bad-1"}).status_code == 422
