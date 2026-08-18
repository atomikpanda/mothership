"""The relay's per-device fleet tokens: the credential the phone carries to read
`GET /hosts`. Stable per label (re-running the mint command must reprint the same
QR, not orphan the phone's copy), revocable per label, and never at rest in
plaintext — the `core.daemon.host_auth.RefreshStore` shape, for the same reasons."""
from __future__ import annotations

import json

import pytest

from mship.core.relay.fleet_token import FleetTokenStore


def _store(tmp_path, **kw):
    return FleetTokenStore(tmp_path / "store", **kw)


def test_issue_is_stable_for_the_same_label(tmp_path):
    # Re-running `mship relay fleet-token --label phone` must reprint the SAME
    # token: a fresh one would silently invalidate the phone already paired.
    store = _store(tmp_path)
    first = store.issue("phone")
    assert store.issue("phone") == first


def test_a_second_store_object_over_the_same_dir_derives_the_same_token(tmp_path):
    # The CLI builds a new store per invocation; stability must be on disk.
    assert _store(tmp_path).issue("phone") == _store(tmp_path).issue("phone")


def test_distinct_labels_get_distinct_tokens(tmp_path):
    store = _store(tmp_path)
    assert store.issue("phone") != store.issue("tablet")


def test_verify_returns_the_label_behind_a_live_token(tmp_path):
    store = _store(tmp_path)
    assert store.verify(store.issue("phone")) == "phone"


@pytest.mark.parametrize(
    "presented",
    ["", "garbage", "nodot", "../../etc/passwd.x", "0123456789abcdef.wrong-secret"],
)
def test_verify_refuses_anything_that_is_not_a_live_token(tmp_path, presented):
    store = _store(tmp_path)
    store.issue("phone")
    assert store.verify(presented) is None


def test_revoke_invalidates_only_that_label(tmp_path):
    store = _store(tmp_path)
    phone, tablet = store.issue("phone"), store.issue("tablet")
    assert store.revoke("phone") is True
    assert store.verify(phone) is None
    assert store.verify(tablet) == "tablet"


def test_revoke_reports_whether_there_was_anything_to_revoke(tmp_path):
    store = _store(tmp_path)
    assert store.revoke("never-minted") is False


def test_reissue_after_revoke_is_a_different_token(tmp_path):
    # Revocation must be real: a re-mint under the same label cannot resurrect
    # the credential the operator just killed.
    store = _store(tmp_path)
    revoked = store.issue("phone")
    store.revoke("phone")
    fresh = store.issue("phone")
    assert fresh != revoked
    assert store.verify(revoked) is None
    assert store.verify(fresh) == "phone"


def test_the_plaintext_token_is_never_written_to_disk(tmp_path):
    store = _store(tmp_path)
    token = store.issue("phone")
    secret = token.split(".", 1)[1]
    for path in (tmp_path / "store").rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes().decode("utf-8", "replace")


def test_token_material_is_owner_only_on_disk(tmp_path):
    store = _store(tmp_path)
    store.issue("phone")
    for path in (tmp_path / "store").rglob("*"):
        if path.is_file() and not path.name.endswith(".lock"):
            assert path.stat().st_mode & 0o077 == 0, path


def test_a_corrupt_document_does_not_brick_the_store(tmp_path):
    store = _store(tmp_path)
    token = store.issue("phone")
    store.path.write_text("{not json")
    assert store.verify(token) is None            # unverifiable, but no raise
    assert store.issue("phone")                   # and it self-heals


def test_labels_are_bounded_so_the_document_cannot_grow_without_limit(tmp_path):
    store = _store(tmp_path, max_labels=2)
    store.issue("a")
    store.issue("b")
    fresh = store.issue("c")
    # The credential just handed back is never the one the cap drops.
    assert store.verify(fresh) == "c"
    assert len(json.loads(store.path.read_text())["labels"]) == 2
