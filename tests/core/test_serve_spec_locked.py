from datetime import datetime, timezone
from pathlib import Path

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


def test_serve_get_locked_spec_returns_marker_not_error(tmp_path: Path):
    _write_encrypted_spec(tmp_path)
    spec_key.keyfile_path(tmp_path).unlink()
    resp = _client(tmp_path).get("/specs/locked-one")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "locked-one" and data["locked"] is True
    assert "SECRET" not in resp.text
