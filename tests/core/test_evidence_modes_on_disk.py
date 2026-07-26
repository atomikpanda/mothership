from mship.core.evidence_store import (
    ENC_SUFFIX,
    evidence_dir,
    resolve_ref,
    store_artifact,
)


def _png(tmp_path):
    p = tmp_path / "screen.png"; p.write_bytes(b"\x89PNG plaintext marker")
    return p


def test_published_mode_writes_plaintext(tmp_path):
    ref = store_artifact(tmp_path, "s", _png(tmp_path), mode="published")
    assert (evidence_dir(tmp_path, "s") / ref).read_bytes().endswith(b"marker")


def test_local_mode_writes_plaintext_and_no_gitignore_of_its_own(tmp_path):
    """The whole store lives under the gitignored `.mothership/`, so `local`
    needs no ignore entry of its own — writing one would only add a stray line
    to the operator's .gitignore for no gain."""
    ref = store_artifact(tmp_path, "s", _png(tmp_path), mode="local")
    assert (evidence_dir(tmp_path, "s") / ref).read_bytes().endswith(b"marker")
    assert not (tmp_path / ".gitignore").exists()


def test_encrypted_mode_writes_ciphertext_with_enc_suffix(tmp_path):
    ref = store_artifact(tmp_path, "s", _png(tmp_path), mode="encrypted")
    assert ref.endswith(ENC_SUFFIX)
    raw = (evidence_dir(tmp_path, "s") / ref).read_bytes()
    assert b"marker" not in raw
    # A regression where `store_artifact` copies the plaintext IN ADDITION TO
    # encrypting it would leave a plaintext sibling in the same directory —
    # checking only the `.enc` file's own bytes wouldn't catch that. Walk
    # every file under the evidence dir, not just the one named `ref`.
    for f in evidence_dir(tmp_path, "s").rglob("*"):
        if f.is_file():
            assert b"marker" not in f.read_bytes(), f"plaintext leaked into {f}"


def test_encrypted_ref_round_trips_through_resolve_ref(tmp_path):
    """An encrypted ref must remain resolvable, or encrypted-mode evidence is
    unservable — and that would not surface until someone set the mode."""
    ref = store_artifact(tmp_path, "s", _png(tmp_path), mode="encrypted")
    assert resolve_ref(tmp_path, "s", ref).is_file()
