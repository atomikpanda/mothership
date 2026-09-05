import os

import pytest

from mship.core.evidence_store import (
    BadEvidenceRef,
    evidence_dir,
    resolve_ref,
    store_artifact,
)


def _stored(tmp_path):
    src = tmp_path / "screen.png"; src.write_bytes(b"bytes")
    return store_artifact(tmp_path, "s", src, mode="published")


def test_resolves_a_stored_ref(tmp_path):
    ref = _stored(tmp_path)
    assert resolve_ref(tmp_path, "s", ref).read_bytes() == b"bytes"


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "/etc/passwd",
    "..%2Fescape.png",
    "sub/dir.png",
    "no-extension",
    "deadbeef.exe",
    "",
])
def test_refuses_malformed_or_escaping_refs(tmp_path, bad):
    _stored(tmp_path)
    with pytest.raises(BadEvidenceRef):
        resolve_ref(tmp_path, "s", bad)


def test_refuses_a_well_formed_hash_with_an_unserved_extension(tmp_path):
    """`deadbeef.exe` is refused for its hash length; a full-length hash with a
    bad extension must still be refused, by the extension check."""
    _stored(tmp_path)
    with pytest.raises(BadEvidenceRef):
        resolve_ref(tmp_path, "s", "deadbeefcafe.exe")


def test_refuses_a_ref_with_a_trailing_newline(tmp_path):
    """A regex anchored with `$` would accept this; the ref must be exact."""
    ref = _stored(tmp_path)
    with pytest.raises(BadEvidenceRef):
        resolve_ref(tmp_path, "s", ref + "\n")


@pytest.mark.parametrize(
    ("spec_id", "ref_suffix"),
    [("s\0", ""), ("s", "\0")],
)
def test_refuses_embedded_nul_in_paths_before_realpath(
    tmp_path, monkeypatch, spec_id, ref_suffix
):
    """NUL belongs to the request boundary, not the OS path API."""
    ref = _stored(tmp_path)

    def realpath_called(path):
        pytest.fail(f"unexpected realpath for {path!r}")

    monkeypatch.setattr(os.path, "realpath", realpath_called)
    with pytest.raises(BadEvidenceRef):
        resolve_ref(tmp_path, spec_id, ref + ref_suffix)


def test_refuses_a_symlink_pointing_out_of_the_store(tmp_path):
    _stored(tmp_path)
    secret = tmp_path / "secret.png"; secret.write_bytes(b"nope")
    link = evidence_dir(tmp_path, "s") / "aaaaaaaaaaaa.png"
    os.symlink(secret, link)
    with pytest.raises(BadEvidenceRef):
        resolve_ref(tmp_path, "s", "aaaaaaaaaaaa.png")


def test_refuses_a_symlink_pointing_inside_the_store(tmp_path):
    """Nothing in the store is legitimately a symlink: the ref attests the
    content hash of a regular file. Refuse links regardless of target so
    containment never depends on where one happens to point."""
    ref = _stored(tmp_path)
    link = evidence_dir(tmp_path, "s") / "bbbbbbbbbbbb.png"
    os.symlink(evidence_dir(tmp_path, "s") / ref, link)
    with pytest.raises(BadEvidenceRef):
        resolve_ref(tmp_path, "s", "bbbbbbbbbbbb.png")


def test_resolves_when_the_workspace_root_is_reached_through_a_symlink(tmp_path):
    """`root` is realpath'd, so a symlinked ancestor (macOS /tmp) must not make
    a legitimately stored ref look like an escape."""
    real = tmp_path / "real"; real.mkdir()
    ref = _stored(real)
    alias = tmp_path / "alias"
    os.symlink(real, alias)
    assert resolve_ref(alias, "s", ref).read_bytes() == b"bytes"


@pytest.mark.parametrize("bad_spec", ["../other-spec", "../../..", "a/b", ""])
def test_refuses_a_spec_id_that_escapes_the_evidence_store(tmp_path, bad_spec):
    """`spec_id` also arrives from the request path, so one spec's ref must not
    be readable under another spec's id."""
    ref = _stored(tmp_path)
    other = evidence_dir(tmp_path, "other-spec")
    other.mkdir(parents=True)
    (other / ref).write_bytes(b"someone else's evidence")
    with pytest.raises(BadEvidenceRef):
        resolve_ref(tmp_path, bad_spec, ref)


def test_missing_ref_raises(tmp_path):
    _stored(tmp_path)
    with pytest.raises(BadEvidenceRef):
        resolve_ref(tmp_path, "s", "ffffffffffff.png")
