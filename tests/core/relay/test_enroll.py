import pytest

from mship.core.relay.enroll import validate_pubkey, fingerprint, sanitize_label
from mship.core.relay.enroll import RequestStore, PendingCapReached, NotPending

_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAAAAAA host"


def test_validate_accepts_ssh_key():
    assert validate_pubkey(_PUB)
    assert validate_pubkey("ssh-rsa AAAAB3NzaC1yc2EAAAAD host")


def test_validate_rejects_junk():
    assert not validate_pubkey("not a key")
    assert not validate_pubkey("")
    assert not validate_pubkey("ssh-ed25519 !!!notbase64!!!")
    assert not validate_pubkey("rm -rf /")


def test_validate_rejects_multiline_injection():
    # A crafted second line must not be smuggled into the authorized_keys allowlist.
    assert not validate_pubkey(_PUB + "\n" + _PUB.replace("host", "evil"))
    assert not validate_pubkey(_PUB + "\r\n" + _PUB.replace("host", "evil"))


def test_fingerprint_is_stable_sha256():
    fp = fingerprint(_PUB)
    assert fp.startswith("SHA256:")
    assert fp == fingerprint(_PUB + "  different-comment")  # body only


def test_fingerprint_rejects_non_key():
    with pytest.raises(ValueError):
        fingerprint("ssh-ed25519")


def test_sanitize_label_is_traversal_proof():
    assert sanitize_label("../../etc/passwd") == "etc-passwd"
    assert sanitize_label("My Laptop!") == "my-laptop"
    assert sanitize_label("") == "device"
    s = sanitize_label("a/" * 100)
    assert "/" not in s and ".." not in s and len(s) <= 40


# ---------------------------------------------------------------------------
# Task 2: RequestStore tests
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _store(tmp_path, ttl=1800, clock=None, cap=50):
    return RequestStore(tmp_path / "store", ttl_seconds=ttl, max_pending=cap, clock=clock or _Clock())


def test_create_then_pending_then_approve_writes_allowlist(tmp_path):
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()
    s = _store(tmp_path)
    rid = s.create(_PUB, "my-laptop")
    assert s.get(rid) == "pending"
    assert [r["id"] for r in s.list_pending()] == [rid]
    s.approve(rid, pubkeys)
    assert s.get(rid) == "approved"
    written = list(pubkeys.glob("*.pub"))
    assert len(written) == 1 and written[0].read_text().strip() == _PUB.strip()
    assert s.list_pending() == []  # no longer pending


def test_deny_resolves_without_touching_allowlist(tmp_path):
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()
    s = _store(tmp_path)
    rid = s.create(_PUB, "h")
    s.deny(rid)
    assert s.get(rid) == "denied"
    assert list(pubkeys.glob("*.pub")) == []


def test_expiry_after_ttl(tmp_path):
    clock = _Clock(1000.0)
    s = _store(tmp_path, ttl=1800, clock=clock)
    rid = s.create(_PUB, "h")
    clock.t = 1000.0 + 1801  # past TTL
    assert s.list_pending() == []
    assert s.get(rid) == "expired"
    with pytest.raises(NotPending):
        s.approve(rid, tmp_path)


def test_pending_cap_enforced(tmp_path):
    s = _store(tmp_path, cap=2)
    s.create(_PUB, "a")
    s.create(_PUB, "b")
    with pytest.raises(PendingCapReached):
        s.create(_PUB, "c")


def test_same_hostname_does_not_clobber(tmp_path):
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()
    s = _store(tmp_path)
    r1 = s.create(_PUB, "laptop")
    s.approve(r1, pubkeys)
    r2 = s.create(_PUB, "laptop")
    s.approve(r2, pubkeys)
    assert len(list(pubkeys.glob("*.pub"))) == 2  # unique filenames


# ---------------------------------------------------------------------------
# Task 2 hardening (security review of commit 5886872)
# ---------------------------------------------------------------------------


def test_create_rejects_multiline_pubkey_and_writes_nothing(tmp_path):
    # The store is the security boundary: a multi-line pubkey must be rejected at
    # create() so a crafted second line can never reach the pubkeys allowlist.
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()
    s = _store(tmp_path)
    evil = _PUB + "\n" + _PUB.replace("host", "evil")
    with pytest.raises(ValueError):
        s.create(evil, "h")
    assert s.list_pending() == []
    assert list((tmp_path / "store" / "pending").glob("*.json")) == []
    assert list(pubkeys.glob("*.pub")) == []


def test_deny_sweeps_so_expired_resolves_as_expired(tmp_path):
    clock = _Clock(1000.0)
    s = _store(tmp_path, ttl=1800, clock=clock)
    rid = s.create(_PUB, "h")
    clock.t = 1000.0 + 1801  # past TTL
    with pytest.raises(NotPending):
        s.deny(rid)
    assert s.get(rid) == "expired"  # not "denied"


def test_corrupt_pending_file_does_not_brick_store(tmp_path):
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()
    s = _store(tmp_path)
    good = s.create(_PUB, "good")
    pending_dir = tmp_path / "store" / "pending"
    (pending_dir / "deadbeef.json").write_text("{ truncated not json")
    # A corrupt sibling must not raise from list/get/approve; it is quarantined.
    assert [r["id"] for r in s.list_pending()] == [good]
    assert s.get(good) == "pending"
    s.approve(good, pubkeys)
    assert s.get(good) == "approved"
    assert list(pending_dir.glob("*.json.corrupt"))  # bad file moved aside
