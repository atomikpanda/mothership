from mship.core.evidence_store import (
    ENC_SUFFIX,
    evidence_dir,
    resolve_ref,
    store_artifact,
)


def _png(tmp_path):
    p = tmp_path / "screen.png"; p.write_bytes(b"\x89PNG plaintext marker")
    return p


def test_committed_mode_writes_plaintext(tmp_path):
    ref = store_artifact(tmp_path, "s", _png(tmp_path), mode="committed")
    assert (evidence_dir(tmp_path, "s") / ref).read_bytes().endswith(b"marker")


def test_local_mode_gitignores_the_evidence_dir(tmp_path):
    store_artifact(tmp_path, "s", _png(tmp_path), mode="local")
    assert "specs/evidence/" in (tmp_path / ".gitignore").read_text()


def test_encrypted_mode_writes_ciphertext_with_enc_suffix(tmp_path):
    ref = store_artifact(tmp_path, "s", _png(tmp_path), mode="encrypted")
    assert ref.endswith(ENC_SUFFIX)
    raw = (evidence_dir(tmp_path, "s") / ref).read_bytes()
    assert b"marker" not in raw


def test_encrypted_ref_round_trips_through_resolve_ref(tmp_path):
    """An encrypted ref must remain resolvable, or encrypted-mode evidence is
    unservable — and that would not surface until someone set the mode."""
    ref = store_artifact(tmp_path, "s", _png(tmp_path), mode="encrypted")
    assert resolve_ref(tmp_path, "s", ref).is_file()
