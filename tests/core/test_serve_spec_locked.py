from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mship.core import spec_key
from mship.core.config import WorkspaceConfig
from mship.core.serve import create_app
from mship.core.spec import Spec
from mship.core.spec_storage import SpecStorage
from mship.core.spec_store import SPECS_DIRNAME, SpecStore


class _NullState:
    def load(self):
        from mship.core.state import WorkspaceState
        return WorkspaceState(tasks={})


def _write_encrypted_spec(root: Path) -> None:
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    spec = Spec(id="locked-one", title="Locked one", status="needs_review",
                created_at=now, updated_at=now, body="## Problem\n\nSECRET\n")
    storage = SpecStorage(root / SPECS_DIRNAME, mode="encrypted", workspace_root=root)
    SpecStore(root / SPECS_DIRNAME, storage=storage).save(spec)


def _client(root: Path) -> TestClient:
    cfg = WorkspaceConfig(workspace="demo", spec_storage="encrypted")
    app = create_app(
        specs_dir=root / SPECS_DIRNAME,
        state_manager=_NullState(),
        log_manager=None,
        workspace_root=root,
        config=cfg,
    )
    return TestClient(app)


def test_serve_decrypts_specs_with_key(tmp_path: Path):
    _write_encrypted_spec(tmp_path)
    body = _client(tmp_path).get("/specs").json()
    row = next(s for s in body if s["id"] == "locked-one")
    assert row["title"] == "Locked one"
    assert row.get("locked") is False


def test_serve_shows_locked_state_without_key(tmp_path: Path):
    _write_encrypted_spec(tmp_path)
    spec_key.keyfile_path(tmp_path).unlink()
    body = _client(tmp_path).get("/specs").json()
    row = next(s for s in body if s["id"] == "locked-one")
    assert row["locked"] is True
    assert row["status"] == "locked"
    assert row["title"] is None
    # No ciphertext leaked into the response.
    assert "gAAAA" not in str(body)


@pytest.mark.parametrize("inbox", ["active", "archived"])
def test_serve_excludes_locked_specs_from_explicit_inbox_filters(tmp_path: Path, inbox: str):
    _write_encrypted_spec(tmp_path)
    spec_key.keyfile_path(tmp_path).unlink()

    assert _client(tmp_path).get("/specs", params={"inbox": inbox}).json() == []


def test_serve_get_locked_spec_returns_marker_not_error(tmp_path: Path):
    _write_encrypted_spec(tmp_path)
    spec_key.keyfile_path(tmp_path).unlink()
    resp = _client(tmp_path).get("/specs/locked-one")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "locked-one" and data["locked"] is True
    assert "SECRET" not in resp.text


def test_serve_rejects_inbox_mutation_for_locked_spec_without_leaking_content(tmp_path: Path):
    _write_encrypted_spec(tmp_path)
    spec_key.keyfile_path(tmp_path).unlink()

    response = _client(tmp_path).post(
        "/specs/locked-one/inbox/archive", json={"mutation_id": "device-archive"},
    )

    assert response.status_code == 409
    assert "locked" in response.json()["detail"]
    assert "SECRET" not in response.text


def test_serve_create_refuses_duplicate_locked_spec_as_conflict(tmp_path: Path):
    _write_encrypted_spec(tmp_path)
    spec_key.keyfile_path(tmp_path).unlink()

    response = _client(tmp_path).post("/specs", json={"id": "locked-one", "title": "Replacement"})

    assert response.status_code == 409
    assert "locked" in response.json()["detail"]


def test_serve_create_reports_encrypted_physical_path(tmp_path: Path):
    response = _client(tmp_path).post(
        "/specs", json={"id": "new-secret", "title": "New secret"},
    )

    assert response.status_code == 200
    assert response.json()["path"].endswith("new-secret.md.enc")
