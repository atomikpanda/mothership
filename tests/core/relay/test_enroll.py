from pathlib import Path

import pytest

from mship.core.relay.enroll import validate_pubkey, fingerprint, sanitize_label
from mship.core.relay.enroll import RequestStore, PendingCapReached, NotPending

_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAAAAAA host"


def _key(tag: str) -> str:
    """A distinct (shape-valid) public key per tag — enrollment is deduped by
    key fingerprint, so a test about *counts* needs distinct keys."""
    body = "AAAAC3NzaC1lZDI1NTE5AAAAI" + tag.ljust(4, "Z") * 6
    return f"ssh-ed25519 {body}A host"


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
    # Distinct keys: same-key posts are deduped, so the cap can only be reached
    # by distinct devices.
    s = _store(tmp_path, cap=2)
    s.create(_key("a"), "a")
    s.create(_key("b"), "b")
    with pytest.raises(PendingCapReached):
        s.create(_key("c"), "c")


def test_same_hostname_does_not_clobber(tmp_path):
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()
    s = _store(tmp_path)
    r1 = s.create(_key("first"), "laptop")
    s.approve(r1, pubkeys)
    r2 = s.create(_key("second"), "laptop")
    s.approve(r2, pubkeys)
    assert len(list(pubkeys.glob("*.pub"))) == 2  # unique filenames


def test_reposting_an_approved_key_returns_its_resolved_request(tmp_path):
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()
    store = _store(tmp_path)
    rid = store.create(_PUB, "laptop")
    store.approve(rid, pubkeys)

    assert store.create(_PUB, "laptop") == rid
    assert store.list_pending() == []


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


def test_approve_and_deny_on_corrupt_own_record_raise_cleanly(tmp_path):
    """A truncated/corrupt pending file at approve/deny time → NotPending, not a traceback."""
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()
    s = _store(tmp_path)
    pending_dir = tmp_path / "store" / "pending"

    rid = s.create(_PUB, "laptop")
    (pending_dir / f"{rid}.json").write_text("{ truncated")
    with pytest.raises(NotPending):
        s.approve(rid, pubkeys)
    assert list(pubkeys.glob("*.pub")) == []  # nothing written to the allowlist

    rid2 = s.create(_PUB, "laptop2")
    (pending_dir / f"{rid2}.json").write_text("not json at all")
    with pytest.raises(NotPending):
        s.deny(rid2)


# --- idempotent enrollment (the daemon re-posts on a schedule) --------------


def test_create_is_idempotent_per_key_fingerprint(tmp_path):
    # The daemon re-posts its enroll request every ENROLL_REPOST_INTERVAL_S while
    # awaiting approval. Without dedupe, one unapproved host fills the 50-slot cap
    # in under 9 hours and 429s enrollment for the whole fleet.
    store = RequestStore(tmp_path)
    rid = store.create(_PUB, "vm-alpha")
    for _ in range(10):
        assert store.create(_PUB, "vm-alpha") == rid
    assert len(store.list_pending()) == 1
    assert len(list((tmp_path / "pending").glob("*.json"))) == 1


def test_dedupe_is_by_key_not_hostname(tmp_path):
    store = RequestStore(tmp_path)
    rid = store.create(_PUB, "vm-alpha")
    # A renamed host re-posting the same key is still one request…
    assert store.create(_PUB, "vm-alpha-renamed") == rid
    # …and a different key is a different request.
    other = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISecondKeyBodyBBBBBBBBBBBBBBBBBBBBBBBB two"
    assert store.create(other, "vm-beta") != rid
    assert len(store.list_pending()) == 2


def test_a_re_post_renews_the_pending_ttl_without_changing_request_id(tmp_path):
    # The daemon re-post interval is shorter than this TTL specifically so an
    # unattended request stays approvable while the daemon is still alive.
    now = [1000.0]
    store = RequestStore(tmp_path, ttl_seconds=100, clock=lambda: now[0])
    rid = store.create(_PUB, "vm-alpha")

    now[0] = 1060.0
    assert store.create(_PUB, "vm-alpha") == rid
    assert store.list_pending()[0]["created_at"] == 1060.0

    now[0] = 1159.0
    assert store.get(rid) == "pending"
    now[0] = 1160.0
    assert store.get(rid) == "expired"


def test_a_resolved_request_does_not_block_a_new_one(tmp_path):
    # Dedupe is scoped to PENDING records: once denied, the same key may re-enroll.
    store = RequestStore(tmp_path)
    rid = store.create(_PUB, "vm-alpha")
    store.deny(rid)
    assert store.create(_PUB, "vm-alpha") != rid


def _post_same_key(base_dir: str, barrier):
    """Subprocess body: one daemon re-posting its enroll request. The barrier
    puts every writer inside the scan-then-write window at once — without it
    process startup staggers them and the race never reproduces."""
    store = RequestStore(Path(base_dir))
    barrier.wait(timeout=30)
    store.create(_PUB, "vm-alpha")


def test_concurrent_re_posts_of_one_key_leave_exactly_one_record(tmp_path):
    # The dedupe scan and the write must be one atomic step: the enroll app
    # serves sync endpoints on a threadpool, so two re-posts of one key really
    # do interleave. Driven with processes (the `test_state_lock.py` pattern),
    # which flock serializes exactly as it does threads.
    import multiprocessing

    store = RequestStore(tmp_path)
    # Unrelated pending records widen the scan the writers must complete before
    # they write, so the interleave is reliable rather than a coin flip.
    for i in range(20):
        store.create(_key(f"o{i}"), f"other-{i}")
    writers = 12
    barrier = multiprocessing.Barrier(writers)
    procs = [
        multiprocessing.Process(target=_post_same_key, args=(str(tmp_path), barrier))
        for _ in range(writers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    assert len(store.list_pending()) == 21
    assert len(list((tmp_path / "pending").glob("*.json"))) == 21
