from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mship.core import spec_key
from mship.core.config import WorkspaceConfig
from mship.core.serve import create_app
from mship.core.spec import Spec
from mship.core.spec_storage import SpecLocked, SpecStorage
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

def test_serve_omits_duplicate_readable_spec_ids_but_preserves_healthy_rows(
    tmp_path: Path,
):
    _write_encrypted_spec(tmp_path)
    specs_dir = tmp_path / SPECS_DIRNAME
    original = next(specs_dir.glob("*-locked-one.md.enc"))
    original.with_name("2026-07-23-locked-one.md.enc").write_bytes(original.read_bytes())
    healthy = Spec(
        id="healthy",
        title="Healthy",
        status="draft",
        created_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    storage = SpecStorage(specs_dir, mode="encrypted", workspace_root=tmp_path)
    SpecStore(specs_dir, storage=storage).save(healthy)

    body = _client(tmp_path).get("/specs").json()

    assert [row["id"] for row in body] == ["healthy"]


def test_serve_omits_duplicate_locked_ids_but_preserves_healthy_plaintext(
    tmp_path: Path,
):
    _write_encrypted_spec(tmp_path)
    specs_dir = tmp_path / SPECS_DIRNAME
    original = next(specs_dir.glob("*-locked-one.md.enc"))
    original.with_name("2026-07-23-locked-one.md.enc").write_bytes(original.read_bytes())
    healthy = Spec(
        id="healthy",
        title="Healthy",
        status="draft",
        created_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    plaintext = SpecStorage(specs_dir, mode="committed", workspace_root=tmp_path)
    SpecStore(specs_dir, storage=plaintext).save(healthy)
    spec_key.keyfile_path(tmp_path).unlink()

    body = _client(tmp_path).get("/specs").json()

    assert [row["id"] for row in body] == ["healthy"]


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


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/specs/locked-one/review", None),
        ("post", "/specs/locked-one/questions", {"text": "Why?"}),
    ],
)
def test_serve_review_and_write_paths_report_locked_spec_as_conflict(
    tmp_path: Path, method: str, path: str, body: dict | None,
):
    _write_encrypted_spec(tmp_path)
    spec_key.keyfile_path(tmp_path).unlink()

    response = _client(tmp_path).request(method, path, json=body)

    assert response.status_code == 409
    assert "locked" in response.json()["detail"]

@pytest.mark.parametrize("spec_id", [".hidden", "%00"])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/specs/{spec_id}/review", None),
        (
            "post",
            "/specs/{spec_id}/verdict",
            {"criterion_id": "ac-1", "verdict": "approved"},
        ),
        (
            "post",
            "/specs/{spec_id}/prose-verdict",
            {"section_id": "problem", "verdict": "approved"},
        ),
        (
            "post",
            "/specs/{spec_id}/evidence",
            {"criterion_id": "ac-1", "ref": "test:tests/core/test_serve_spec_locked.py"},
        ),
        (
            "post",
            "/specs/{spec_id}/apply",
            {
                "draft": {
                    "problem": "Problem",
                    "user_story": "User story",
                    "approach": "Approach",
                },
            },
        ),
        ("post", "/specs/{spec_id}/questions", {"text": "Why?"}),
        ("post", "/specs/{spec_id}/questions/q-1/answer", {"answer": "Because."}),
    ],
)
def test_serve_strict_routes_reject_unsafe_spec_ids_without_a_server_error(
    tmp_path: Path, spec_id: str, method: str, path: str, body: dict | None,
):
    response = _client(tmp_path).request(
        method, path.format(spec_id=spec_id), json=body,
    )

    assert response.status_code == 422
    assert "unsafe spec id" in response.json()["detail"]


@pytest.mark.parametrize(
    ("helper", "path", "body"),
    [
        ("approve_spec", "/specs/locked-one/approve", {"bypass_gate": True}),
        (
            "request_changes_spec",
            "/specs/locked-one/request-changes",
            {"reason": "Needs revision"},
        ),
    ],
)
def test_transition_save_race_maps_locked_storage_to_conflict(
    tmp_path: Path, monkeypatch, helper: str, path: str, body: dict,
):
    import mship.core.serve as serve

    _write_encrypted_spec(tmp_path)

    def raise_locked(*_args, **_kwargs):
        raise SpecLocked("locked-one")

    monkeypatch.setattr(serve, helper, raise_locked)
    response = _client(tmp_path).post(path, json=body)

    assert response.status_code == 409
    assert response.json()["detail"] == "spec 'locked-one' is locked"
