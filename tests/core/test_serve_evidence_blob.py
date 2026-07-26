"""GET /specs/{spec_id}/evidence/{name}/blob — the read-only artifact bytes.

Both path parameters arrive from the network, so the interesting cases here are
the ones that must NOT resolve. All of that validation lives in
`core/evidence_store.py::resolve_ref`; these tests pin the route's contract
(status codes, content-type, never ciphertext), not a second copy of the rules.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mship.core import spec_key
from mship.core.serve import create_app
from mship.core.spec import AcceptanceCriterion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager

TOKEN = "sekrit"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PNG_BYTES = b"\x89PNG fake bytes"


def _app(tmp_path: Path, auth_token: str | None = None, config=None):
    return create_app(
        specs_dir=tmp_path / "specs",
        state_manager=StateManager(tmp_path / ".mothership"),
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
        auth_token=auth_token,
        config=config,
    )


def _client(tmp_path: Path, config=None) -> TestClient:
    return TestClient(_app(tmp_path, auth_token=TOKEN, config=config))


def _spec(now) -> Spec:
    return Spec(
        id="dq", title="Decision queue", status="needs_review",
        created_at=now, updated_at=now, task_slug="dq",
        body=render_body("the problem", "as a user", "the approach"),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="view questions")],
    )


def _seed_spec(tmp_path: Path) -> None:
    SpecStore(tmp_path / "specs").save(_spec(datetime(2026, 7, 26, tzinfo=timezone.utc)))


def _seed_encrypted_spec(tmp_path: Path) -> None:
    """A genuinely encrypted workspace: the SPEC FILE itself is ciphertext, not
    just the artifact. This is what `spec_storage: encrypted` produces, and the
    only shape in which a missing key locks the spec as well as its evidence."""
    from mship.core.spec_storage import SpecStorage

    storage = SpecStorage(tmp_path / "specs", mode="encrypted", workspace_root=tmp_path)
    SpecStore(tmp_path / "specs", storage=storage).save(
        _spec(datetime(2026, 7, 26, tzinfo=timezone.utc))
    )


def _seed_evidence(tmp_path: Path, mode: str = "committed") -> str:
    """Store an artifact the way the CLI does, so the ref under test is the real
    content-hashed name rather than one this test invented."""
    from mship.core.evidence_store import store_artifact

    src = tmp_path / "screen.png"
    src.write_bytes(PNG_BYTES)
    return store_artifact(tmp_path, "dq", src, mode=mode)


def test_blob_returns_stored_bytes_and_content_type(tmp_path: Path):
    _seed_spec(tmp_path)
    ref = _seed_evidence(tmp_path)
    r = _client(tmp_path).get(f"/specs/dq/evidence/{ref}/blob", headers=AUTH)
    assert r.status_code == 200
    assert r.content == PNG_BYTES
    assert r.headers["content-type"].startswith("image/png")


def test_blob_requires_the_bearer(tmp_path: Path):
    _seed_spec(tmp_path)
    ref = _seed_evidence(tmp_path)
    # The app-wide dependency covers this route like every other one.
    assert _client(tmp_path).get(f"/specs/dq/evidence/{ref}/blob").status_code == 401


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",       # traversal, encoded by the client into %2F segments
    "sub%2Fdir.png",          # an encoded separator, decoded before routing
    "ffffffffffff.png",       # well-formed but absent
    "x",                      # not a ref at all
    "deadbeefcafe.exe",       # full-length hash, extension we never serve
    "deadbeefcafe.png%0A",    # trailing newline (why resolve_ref uses fullmatch)
])
def test_unresolvable_ref_is_404_not_403(tmp_path: Path, bad: str):
    # 404 for everything: the route must never confirm what exists on disk.
    _seed_spec(tmp_path)
    _seed_evidence(tmp_path)
    r = _client(tmp_path).get(f"/specs/dq/evidence/{bad}/blob", headers=AUTH)
    assert r.status_code == 404, r.text


def test_traversing_spec_id_is_404(tmp_path: Path):
    """Two distinct shapes, both 404 — the second is the one that reaches us.

    Starlette percent-decodes the request path BEFORE routing, so `..%2Fother`
    becomes two segments and no longer matches the single-segment `{spec_id}`
    path parameter: the router 404s before the handler runs. But `%2E%2E`
    decodes to a bare `..`, which is a legal single segment and DOES arrive in
    the handler — so the handler itself has to refuse it.
    """
    _seed_spec(tmp_path)
    ref = _seed_evidence(tmp_path)
    other = tmp_path / "specs" / "evidence" / "other"
    other.mkdir(parents=True)
    (other / ref).write_bytes(b"another spec's secret screenshot")

    client = _client(tmp_path)
    assert client.get(f"/specs/..%2Fother/evidence/{ref}/blob", headers=AUTH).status_code == 404
    r = client.get(f"/specs/%2E%2E/evidence/{ref}/blob", headers=AUTH)
    assert r.status_code == 404, r.text
    assert b"secret screenshot" not in r.content


def test_locked_spec_reports_locked_rather_than_missing(tmp_path: Path):
    """The primary shape of "no key on this host": `spec_storage: encrypted`,
    so the spec file is ciphertext too and evidence inherits the mode.

    The blob route must report the SAME key-unavailable condition the spec read
    path reports. It used to 404 "no spec" instead, because its pre-check went
    through the spec store's list, which silently skips locked specs.
    """
    from mship.core.config import WorkspaceConfig

    _seed_encrypted_spec(tmp_path)
    ref = _seed_evidence(tmp_path, mode="encrypted")
    spec_key.keyfile_path(tmp_path).unlink()

    client = _client(tmp_path, config=WorkspaceConfig(
        workspace="test-ws", spec_storage="encrypted",
    ))
    # What the spec read path says about this spec, for comparison.
    detail = client.get("/specs/dq", headers=AUTH)
    assert detail.status_code == 200 and detail.json()["locked"] is True

    r = client.get(f"/specs/dq/evidence/{ref}/blob", headers=AUTH)
    assert r.status_code == 409, r.text
    assert "locked" in r.text
    assert PNG_BYTES not in r.content and b"gAAAA" not in r.content


def test_encrypted_blob_without_key_is_locked_not_plaintext(tmp_path: Path):
    # The legal-but-narrower config: plaintext spec, encrypted evidence
    # (`spec_storage: committed` + `evidence_storage: encrypted`).
    _seed_spec(tmp_path)
    ref = _seed_evidence(tmp_path, mode="encrypted")
    spec_key.keyfile_path(tmp_path).unlink()
    r = _client(tmp_path).get(f"/specs/dq/evidence/{ref}/blob", headers=AUTH)
    assert r.status_code == 409, r.text
    assert "locked" in r.text
    # Neither plaintext nor ciphertext leaves the host.
    assert PNG_BYTES not in r.content
    assert b"gAAAA" not in r.content


def test_encrypted_blob_with_key_returns_decrypted_bytes(tmp_path: Path):
    _seed_spec(tmp_path)
    ref = _seed_evidence(tmp_path, mode="encrypted")
    assert ref.endswith(".png.enc")
    r = _client(tmp_path).get(f"/specs/dq/evidence/{ref}/blob", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.content == PNG_BYTES
    # The content-type is the LOGICAL extension's, not `.enc`'s.
    assert r.headers["content-type"].startswith("image/png")


def test_layout_html_is_never_served_as_html(tmp_path: Path):
    """A layout dump is app-under-test content, so serving it as text/html would
    make the API's own origin a stored-XSS sink. Type and nosniff, together."""
    _seed_spec(tmp_path)
    from mship.core.evidence_store import store_artifact
    src = tmp_path / "layout.html"
    src.write_text("<script>alert(1)</script>")
    ref = store_artifact(tmp_path, "dq", src, mode="committed")

    r = _client(tmp_path).get(f"/specs/dq/evidence/{ref}/blob", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_oversized_blob_is_refused(tmp_path: Path, monkeypatch):
    """Serve-time is where the cap actually protects the caller: bytes can reach
    the store without passing store_artifact. The cap VALUE is a judgement call,
    so the test moves it rather than materialising megabytes."""
    from mship.core import evidence_store

    _seed_spec(tmp_path)
    ref = _seed_evidence(tmp_path)
    monkeypatch.setattr(evidence_store, "MAX_EVIDENCE_BYTES", 1)
    r = _client(tmp_path).get(f"/specs/dq/evidence/{ref}/blob", headers=AUTH)
    assert r.status_code == 413, r.text
    assert PNG_BYTES not in r.content


def test_undecryptable_encrypted_blob_is_not_a_500(tmp_path: Path):
    """A `.enc` artifact the current key cannot open (key regenerated after a
    loss, or a restore from another host) is a locked-shaped 409, not a
    traceback."""
    _seed_spec(tmp_path)
    ref = _seed_evidence(tmp_path, mode="encrypted")
    keyfile = spec_key.keyfile_path(tmp_path)
    keyfile.unlink()
    from cryptography.fernet import Fernet
    keyfile.write_bytes(Fernet.generate_key())
    r = _client(tmp_path).get(f"/specs/dq/evidence/{ref}/blob", headers=AUTH)
    assert r.status_code == 409, r.text
    assert PNG_BYTES not in r.content
