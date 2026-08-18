"""Refresh credentials (#471 Task 2): stable per client, revocable, hash-at-rest.

AC11's bound lives here: a network flap re-registers, and re-registration must
re-publish the SAME credential rather than mint a second one — otherwise N
reconnects leave N rows in `~/.mothership/daemon/` and the phone's stored
credential silently becomes one of many.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mship.core.daemon.host_auth import (
    MAX_REFRESH_CLIENTS,
    REFRESH_TTL_S,
    RefreshStore,
)
from mship.core.daemon.paths import daemon_state_dir, host_refresh_path

HOST = "hst-20260817120000-abcd1234"


class _Wall:
    """Fake wall clock; refresh credentials are long-lived, so wall time (with
    the shared skew grace) is the right bound for them."""

    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _store(home: Path, clock=None, **kw) -> RefreshStore:
    return RefreshStore(home, clock=clock or _Wall(), **kw)


def _records(home: Path) -> dict:
    return json.loads(host_refresh_path(home).read_text())["clients"]


def test_issue_verify_roundtrip(tmp_path: Path):
    store = _store(tmp_path)
    cred = store.issue_refresh(host_id=HOST, client="phone-a")
    grant = store.verify_refresh(cred)
    assert grant is not None
    assert grant.client == "phone-a"
    assert grant.host_id == HOST


def test_only_the_hash_is_persisted(tmp_path: Path):
    store = _store(tmp_path)
    cred = store.issue_refresh(host_id=HOST, client="phone-a")
    _cid, secret = cred.split(".", 1)
    assert secret not in host_refresh_path(tmp_path).read_text()


def test_reissue_returns_the_same_credential_and_writes_nothing(tmp_path: Path):
    """AC11: N re-registrations leave the file byte-identical and produce
    exactly one entry."""
    store = _store(tmp_path)
    first = store.issue_refresh(host_id=HOST, client="phone-a")
    after_first = host_refresh_path(tmp_path).read_bytes()

    for _ in range(5):
        assert store.issue_refresh(host_id=HOST, client="phone-a") == first

    assert host_refresh_path(tmp_path).read_bytes() == after_first
    assert len(_records(tmp_path)) == 1


def test_reissue_is_stable_across_store_instances(tmp_path: Path):
    """A daemon restart between registrations must not re-mint either."""
    first = _store(tmp_path).issue_refresh(host_id=HOST, client="phone-a")
    after_first = host_refresh_path(tmp_path).read_bytes()
    assert _store(tmp_path).issue_refresh(host_id=HOST, client="phone-a") == first
    assert host_refresh_path(tmp_path).read_bytes() == after_first


def test_distinct_clients_get_distinct_credentials(tmp_path: Path):
    store = _store(tmp_path)
    a = store.issue_refresh(host_id=HOST, client="phone-a")
    b = store.issue_refresh(host_id=HOST, client="phone-b")
    assert a != b
    assert store.verify_refresh(a).client == "phone-a"
    assert store.verify_refresh(b).client == "phone-b"
    assert len(_records(tmp_path)) == 2


def test_revoke_kills_one_client_and_leaves_its_sibling_working(tmp_path: Path):
    store = _store(tmp_path)
    a = store.issue_refresh(host_id=HOST, client="phone-a")
    b = store.issue_refresh(host_id=HOST, client="phone-b")

    assert store.revoke(host_id=HOST, client="phone-a") is True
    assert store.verify_refresh(a) is None
    assert store.verify_refresh(b) is not None
    assert store.revoke(host_id=HOST, client="phone-a") is False  # already gone


def test_reissue_after_revoke_mints_a_new_credential(tmp_path: Path):
    """Revocation must be real: re-registering the same client name cannot
    resurrect the credential the operator just killed."""
    store = _store(tmp_path)
    a = store.issue_refresh(host_id=HOST, client="phone-a")
    store.revoke(host_id=HOST, client="phone-a")
    again = store.issue_refresh(host_id=HOST, client="phone-a")
    assert again != a
    assert store.verify_refresh(a) is None
    assert store.verify_refresh(again) is not None


def test_a_credential_is_scoped_to_its_host(tmp_path: Path):
    store = _store(tmp_path)
    a = store.issue_refresh(host_id=HOST, client="phone-a")
    other = store.issue_refresh(host_id="hst-other", client="phone-a")
    assert a != other
    assert store.verify_refresh(a).host_id == HOST
    assert store.verify_refresh(other).host_id == "hst-other"


def test_verify_rejects_a_wrong_secret(tmp_path: Path):
    store = _store(tmp_path)
    cred = store.issue_refresh(host_id=HOST, client="phone-a")
    cid = cred.split(".", 1)[0]
    assert store.verify_refresh(f"{cid}.wrong") is None


@pytest.mark.parametrize("presented", [
    "", "no-dot", ".", "abc.", "../../../etc/passwd.x", "NOTHEX.s", "deadbeef.s",
])
def test_verify_never_raises_on_hostile_input(tmp_path: Path, presented: str):
    store = _store(tmp_path)
    store.issue_refresh(host_id=HOST, client="phone-a")
    assert store.verify_refresh(presented) is None


def test_verify_never_raises_on_a_corrupt_store(tmp_path: Path):
    store = _store(tmp_path)
    cred = store.issue_refresh(host_id=HOST, client="phone-a")
    host_refresh_path(tmp_path).write_text("{not json")
    assert store.verify_refresh(cred) is None


def test_verify_writes_nothing(tmp_path: Path):
    store = _store(tmp_path)
    cred = store.issue_refresh(host_id=HOST, client="phone-a")
    before = host_refresh_path(tmp_path).read_bytes()
    for _ in range(5):
        assert store.verify_refresh(cred) is not None
    assert host_refresh_path(tmp_path).read_bytes() == before


def test_expired_credentials_are_rejected_and_pruned_on_issue(tmp_path: Path):
    clock = _Wall()
    store = _store(tmp_path, clock=clock)
    stale = store.issue_refresh(host_id=HOST, client="phone-a")
    clock.advance(REFRESH_TTL_S + 86_400)
    assert store.verify_refresh(stale) is None

    store.issue_refresh(host_id=HOST, client="phone-b")
    assert set(r["client"] for r in _records(tmp_path).values()) == {"phone-b"}


def test_the_store_is_capped(tmp_path: Path):
    clock = _Wall()
    store = _store(tmp_path, clock=clock)
    for i in range(MAX_REFRESH_CLIENTS + 3):
        store.issue_refresh(host_id=HOST, client=f"phone-{i}")
        clock.advance(1)  # distinct created_at, so "drop the oldest" is defined
    assert len(_records(tmp_path)) == MAX_REFRESH_CLIENTS


def test_every_issued_credential_verifies_while_the_cap_churns(tmp_path: Path):
    """The cap must never evict the credential it is handing back — including
    the pathological case where the new record is the oldest by tie-break."""
    clock = _Wall()
    store = _store(tmp_path, clock=clock)
    for i in range(MAX_REFRESH_CLIENTS + 5):
        cred = store.issue_refresh(host_id=HOST, client=f"phone-{i}")
        assert store.verify_refresh(cred) is not None, f"evicted its own mint at {i}"


def test_revoke_prunes_expired_siblings(tmp_path: Path):
    clock = _Wall()
    store = _store(tmp_path, clock=clock)
    store.issue_refresh(host_id=HOST, client="stale")
    clock.advance(REFRESH_TTL_S + 86_400)
    store.issue_refresh(host_id=HOST, client="live")
    store.issue_refresh(host_id=HOST, client="doomed")

    assert store.revoke(host_id=HOST, client="doomed") is True
    assert set(r["client"] for r in _records(tmp_path).values()) == {"live"}


# --- the state dir is owner-only, whatever the umask -----------------------


@pytest.fixture
def loose_umask():
    """A permissive umask, so these assertions test the corrective chmod rather
    than the ambient umask (mkdir's mode argument is umask-masked)."""
    previous = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


def test_state_dir_and_store_are_owner_only(tmp_path: Path, loose_umask):
    store = _store(tmp_path)
    cred = store.issue_refresh(host_id=HOST, client="phone-a")
    assert _mode(daemon_state_dir(tmp_path)) == "0o700"
    assert _mode(host_refresh_path(tmp_path)) == "0o600"

    store.verify_refresh(cred)
    store.revoke(host_id=HOST, client="phone-a")
    assert _mode(daemon_state_dir(tmp_path)) == "0o700"
    assert _mode(host_refresh_path(tmp_path)) == "0o600"


def test_a_pre_existing_loose_state_dir_is_tightened(tmp_path: Path, loose_umask):
    state_dir = daemon_state_dir(tmp_path)
    state_dir.mkdir(parents=True)
    state_dir.chmod(0o755)
    _store(tmp_path).issue_refresh(host_id=HOST, client="phone-a")
    assert _mode(state_dir) == "0o700"
