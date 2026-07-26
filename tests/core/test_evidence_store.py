from mship.core.evidence_store import evidence_dir, store_artifact


def test_store_returns_bare_filename_and_writes_bytes(tmp_path):
    src = tmp_path / "screen.png"
    src.write_bytes(b"\x89PNG fake bytes")

    ref = store_artifact(tmp_path, "my-spec", src, mode="committed")

    assert "/" not in ref and "\\" not in ref
    assert ref.endswith(".png")
    landed = evidence_dir(tmp_path, "my-spec") / ref
    assert landed.read_bytes() == b"\x89PNG fake bytes"


def test_identical_content_yields_identical_ref(tmp_path):
    a = tmp_path / "a.png"; a.write_bytes(b"same")
    b = tmp_path / "b.png"; b.write_bytes(b"same")
    assert store_artifact(tmp_path, "s", a, mode="committed") == \
           store_artifact(tmp_path, "s", b, mode="committed")


def test_different_content_yields_different_ref(tmp_path):
    a = tmp_path / "a.png"; a.write_bytes(b"one")
    b = tmp_path / "b.png"; b.write_bytes(b"two")
    assert store_artifact(tmp_path, "s", a, mode="committed") != \
           store_artifact(tmp_path, "s", b, mode="committed")


def test_store_lands_under_specs_evidence_spec_id(tmp_path):
    src = tmp_path / "layout.xml"; src.write_text("<hierarchy/>")
    store_artifact(tmp_path, "my-spec", src, mode="committed")
    assert (tmp_path / "specs" / "evidence" / "my-spec").is_dir()


def test_unsupported_extension_is_refused(tmp_path):
    import pytest
    from mship.core.evidence_store import EvidenceStoreError

    src = tmp_path / "payload.exe"; src.write_bytes(b"MZ")
    with pytest.raises(EvidenceStoreError):
        store_artifact(tmp_path, "s", src, mode="committed")
