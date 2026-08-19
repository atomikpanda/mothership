"""The relay host directory: the relay never asserts identity — every entry it
writes is backed by a signature it verified against the same `pubkeys/`
allowlist sish authenticates against, every freshness decision is stamped by
the relay's own clock, and a second live claimant is arbitrated by probing the
incumbent rather than by trusting a copyable fingerprint."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
from pathlib import Path

import pytest

from mship.core.relay import host_contract
from mship.core.relay.host_directory import (
    ChallengeRefused,
    DuplicateIdentity,
    HostDirectory,
    InvalidHostId,
    SignatureRefused,
    VerificationBusy,
    probe_instance_id,
)

FP_A = "SHA256:keyA"
FP_B = "SHA256:keyB"
MACHINE = "machine-fingerprint-copied-by-cp-a"
SUBDOMAIN = "abc123-a1b2c3"
PUBLIC_URL = f"https://{SUBDOMAIN}.relay.example"


class Clock:
    """Injected relay clock: time only moves when a test moves it."""

    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Prober:
    """Fake arbitration probe: records the URLs probed, returns scripted ids."""

    def __init__(self, answers=None, raises=None):
        self.urls: list[str] = []
        self._answers = answers or {}
        self._raises = raises

    def __call__(self, public_url):
        self.urls.append(public_url)
        if self._raises is not None:
            raise self._raises
        return self._answers.get(public_url)


def _verify(blob, *, signature, identity, allowed_signers, namespace):
    """Stand-in for `ssh_sig.verify_blob`: a signature is `sig:<fingerprint>`
    over the exact expected blob, and only allowlisted keys can verify."""
    assert namespace is host_contract.NAMESPACE
    if identity not in allowed_signers:
        return False
    return signature == f"sig:{identity}:{blob.decode('utf-8', 'replace')}"


def _sign(nonce, payload, fingerprint=FP_A):
    blob = host_contract.signing_blob(nonce, payload)
    return f"sig:{fingerprint}:{blob.decode('utf-8')}"


def _payload(**over):
    payload = {
        "host_id": "hst-20260818120000-aaaaaaaa",
        "instance_id": "inst-1",
        "label": "vm-alpha",
        "key_fingerprint": FP_A,
        "machine_fingerprint": MACHINE,
        "subdomain": SUBDOMAIN,
        "public_url": PUBLIC_URL,
        "mship_version": "1.2.3",
        "capabilities": {"tunnel": True},
        "runner": {"enabled": False, "state": "disabled"},
        "refresh": "refresh-token-1",
    }
    payload.update(over)
    return payload


def _dir(tmp_path, clock=None, probe=None, signers=(FP_A,)):
    return HostDirectory(
        tmp_path,
        relay_domain="relay.example",
        allowed_signers=lambda: "\n".join(signers),
        verify=_verify,
        probe=probe if probe is not None else Prober(),
        clock=clock or Clock(),
    )


def _register(d, payload=None, *, fingerprint=FP_A, signature=None):
    payload = payload if payload is not None else _payload()
    nonce = d.issue_challenge(str(payload["key_fingerprint"]))["nonce"]
    sig = signature if signature is not None else _sign(nonce, payload, fingerprint)
    return d.register(payload, nonce=nonce, signature=sig)


def test_one_approved_key_owns_at_most_one_host_record(tmp_path):
    d = _dir(tmp_path)
    first = _payload()
    second = _payload(
        host_id="hst-20260818120000-bbbbbbbb",
        instance_id="inst-2",
        subdomain="def456-d4e5f6",
        public_url="https://def456-d4e5f6.relay.example",
    )

    _register(d, first)
    with pytest.raises(DuplicateIdentity) as error:
        _register(d, second)
    assert "approved key is already bound to another host_id" in str(error.value)

    assert d.get_host(second["host_id"]) is None
    assert [host["host_id"] for host in d.list_hosts()] == [first["host_id"]]


def test_same_key_different_host_ids_are_serialized_after_nonce_consumption(tmp_path):
    d = _dir(tmp_path)
    first = _payload()
    second = _payload(
        host_id="hst-20260818120000-bbbbbbbb",
        instance_id="inst-2",
        subdomain="def456-d4e5f6",
        public_url="https://def456-d4e5f6.relay.example",
    )
    first_spent = threading.Event()
    release_first = threading.Event()
    consume = d._consume_nonce

    def consume_then_pause(nonce, identity):
        consume(nonce, identity)
        if not first_spent.is_set():
            first_spent.set()
            release_first.wait(timeout=1)

    d._consume_nonce = consume_then_pause
    with ThreadPoolExecutor(max_workers=1) as executor:
        earlier = executor.submit(_register, d, first)
        assert first_spent.wait(timeout=1)
        assert _register(d, second)["host_id"] == second["host_id"]
        release_first.set()
        with pytest.raises(DuplicateIdentity):
            earlier.result()

    assert d.get_host(first["host_id"]) is None
    assert [host["host_id"] for host in d.list_hosts()] == [second["host_id"]]


# --- challenges -------------------------------------------------------------


def test_a_nonce_is_single_use_and_relay_stamped(tmp_path):
    clock = Clock(5_000.0)
    d = _dir(tmp_path, clock=clock)
    ch = d.issue_challenge(FP_A)
    assert ch["issued_at"] == 5_000.0
    assert ch["expires_at"] == 5_000.0 + host_contract.CHALLENGE_TTL_S
    payload = _payload()
    d.register(payload, nonce=ch["nonce"], signature=_sign(ch["nonce"], payload))
    # Replay of the same nonce is refused even with a perfect signature.
    with pytest.raises(ChallengeRefused):
        d.register(payload, nonce=ch["nonce"], signature=_sign(ch["nonce"], payload))


def test_an_expired_nonce_is_refused_on_the_relay_clock(tmp_path):
    clock = Clock()
    d = _dir(tmp_path, clock=clock)
    ch = d.issue_challenge(FP_A)
    clock.t += host_contract.CHALLENGE_TTL_S + 1
    payload = _payload()
    with pytest.raises(ChallengeRefused):
        d.register(payload, nonce=ch["nonce"], signature=_sign(ch["nonce"], payload))
    assert d.get_host(payload["host_id"]) is None


def test_a_nonce_is_claimed_before_it_is_read_so_a_racing_twin_loses(tmp_path):
    """Two registrations quoting one nonce must not BOTH pass the check.

    Reachable in one process: the enroll app serves sync endpoints on a
    threadpool. The interleave is simulated by re-entering `_consume_nonce`
    from inside the read — i.e. a second consumer arriving after the first has
    read the record but before it finished — which an exists()-read-unlink
    sequence would let through.
    """
    d = _dir(tmp_path)
    nonce = d.issue_challenge(FP_A)["nonce"]
    losers = []
    real_read = d._read_challenge

    def racing_read(path):
        rec = real_read(path)
        if not losers:
            try:
                d._consume_nonce(nonce, FP_A)
                losers.append("ADMITTED")
            except ChallengeRefused:
                losers.append("refused")
        return rec

    d._read_challenge = racing_read
    d._consume_nonce(nonce, FP_A)  # the winner
    assert losers == ["refused"]
    assert list((tmp_path / "challenges").glob("*.json")) == []


def test_challenges_are_bounded_to_one_live_record_per_approved_identity(tmp_path):
    clock = Clock()
    d = _dir(tmp_path, clock=clock, signers=(FP_A, FP_B))

    first = d.issue_challenge(FP_A)
    assert d.issue_challenge(FP_A) == first
    assert d.issue_challenge(FP_B)["nonce"] != first["nonce"]
    assert len(list((tmp_path / "challenges").glob("*.json"))) == 2
    with pytest.raises(SignatureRefused):
        d.issue_challenge("SHA256:not-approved")


def test_invalid_signature_does_not_burn_the_shared_identity_challenge(tmp_path):
    d = _dir(tmp_path)
    payload = _payload()
    nonce = d.issue_challenge(FP_A)["nonce"]

    with pytest.raises(SignatureRefused):
        d.register(payload, nonce=nonce, signature="not-a-signature")

    assert (
        d.register(payload, nonce=nonce, signature=_sign(nonce, payload))["host_id"]
        == payload["host_id"]
    )


def test_signature_verification_capacity_refuses_without_spending_nonce(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    verify_calls = 0

    def blocking_verify(*args, **kwargs):
        nonlocal verify_calls
        verify_calls += 1
        entered.set()
        release.wait(timeout=1)
        return False

    d = HostDirectory(
        tmp_path,
        relay_domain="relay.example",
        allowed_signers=lambda: FP_A,
        probe=Prober(),
        verify=blocking_verify,
        max_concurrent_verifications=1,
    )
    payload = _payload()
    nonce = d.issue_challenge(FP_A)["nonce"]

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            d.register, payload, nonce=nonce, signature="forged-first"
        )
        assert entered.wait(timeout=1)
        with pytest.raises(VerificationBusy):
            d.register(payload, nonce=nonce, signature="forged-second")
        assert verify_calls == 1
        assert list((tmp_path / "challenges").glob("*.json"))
        release.set()
        with pytest.raises(SignatureRefused):
            first.result()

    d._verify = _verify
    assert (
        d.register(payload, nonce=nonce, signature=_sign(nonce, payload))["host_id"]
        == payload["host_id"]
    )


def test_two_valid_registrations_sharing_a_nonce_admit_exactly_one(tmp_path):
    d = _dir(tmp_path)
    payload = _payload()
    nonce = d.issue_challenge(FP_A)["nonce"]
    barrier = threading.Barrier(2)

    def verify_together(*args, **kwargs):
        valid = _verify(*args, **kwargs)
        barrier.wait(timeout=1)
        return valid

    d._verify = verify_together

    def register():
        try:
            d.register(payload, nonce=nonce, signature=_sign(nonce, payload))
            return "registered"
        except ChallengeRefused:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: register(), range(2)))

    assert sorted(outcomes) == ["refused", "registered"]


def test_a_claim_stranded_by_a_crash_is_swept_like_any_challenge(tmp_path):
    # A crash between the claiming rename and the spending unlink must not
    # leave a file nothing ever reaps on a public write path.
    clock = Clock()
    d = _dir(tmp_path, clock=clock)
    ch = d.issue_challenge(FP_A)
    stranded = tmp_path / "challenges" / f"{ch['nonce']}.deadbeef.claim"
    (tmp_path / "challenges" / f"{ch['nonce']}.json").rename(stranded)
    d.issue_challenge(FP_A)  # sweeps, but the claim is live
    assert stranded.is_file()
    clock.t += host_contract.CHALLENGE_TTL_S + 1
    d.issue_challenge(FP_A)
    assert not stranded.exists()


def test_an_unknown_nonce_is_refused(tmp_path):
    d = _dir(tmp_path)
    payload = _payload()
    with pytest.raises(ChallengeRefused):
        d.register(
            payload, nonce="never-issued", signature=_sign("never-issued", payload)
        )


# --- the relay must not assert identity -------------------------------------


def test_a_verified_registration_writes_one_entry(tmp_path):
    clock = Clock(9_000.0)
    d = _dir(tmp_path, clock=clock)
    entry = _register(d)
    on_disk = json.loads(
        (tmp_path / "hosts" / f"{_payload()['host_id']}.json").read_text()
    )
    assert on_disk == entry
    assert entry["instance_id"] == "inst-1"
    assert entry["public_url"] == PUBLIC_URL
    assert entry["last_seen"] == 9_000.0 and entry["first_seen"] == 9_000.0
    assert list((tmp_path / "hosts").glob("*.json")) != []


def test_the_store_is_owner_only_because_an_entry_carries_a_refresh_credential(
    tmp_path,
):
    d = _dir(tmp_path)
    _register(d)
    assert oct((tmp_path / "hosts").stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "challenges").stat().st_mode & 0o777) == "0o700"
    entry = tmp_path / "hosts" / f"{_payload()['host_id']}.json"
    assert oct(entry.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("kind", ["unsigned", "wrong-key", "tampered-payload"])
def test_no_entry_is_ever_written_without_a_verified_signature(tmp_path, kind):
    d = _dir(tmp_path)
    payload = _payload()
    nonce = d.issue_challenge(FP_A)["nonce"]
    if kind == "unsigned":
        sig = ""
    elif kind == "wrong-key":
        # Signed by an allowlisted key that is NOT the one the payload claims.
        sig = _sign(nonce, payload, fingerprint=FP_B)
    elif kind == "tampered-payload":
        sig = _sign(nonce, _payload(public_url="https://evil.example"))
    with pytest.raises(SignatureRefused):
        d.register(payload, nonce=nonce, signature=sig)
    assert d.get_host(payload["host_id"]) is None
    assert list((tmp_path / "hosts").glob("*.json")) == []


def test_a_traversal_shaped_host_id_never_touches_the_filesystem(tmp_path):
    d = _dir(tmp_path)
    payload = _payload(host_id="../../evil")
    nonce = d.issue_challenge(FP_A)["nonce"]
    with pytest.raises(InvalidHostId):
        d.register(payload, nonce=nonce, signature=_sign(nonce, payload))
    assert list(tmp_path.rglob("*evil*")) == []
    assert d.get_host("../../evil") is None


# --- AC10: the relay's clock is the only clock that counts ------------------


def test_a_skewed_payload_timestamp_changes_neither_staleness_nor_takeover(tmp_path):
    clock = Clock(10_000.0)
    d = _dir(tmp_path, clock=clock)
    entry = _register(d, _payload(last_seen=0.0, registered_at=10_000.0 + 3_600))
    assert entry["last_seen"] == 10_000.0
    clock.t += host_contract.DIRECTORY_STALE_S - 1
    assert d.list_hosts()[0]["state"] == "online"
    clock.t += 2
    assert d.list_hosts()[0]["state"] == "offline"


# --- AC11: idempotence ------------------------------------------------------


def test_re_registration_by_the_same_identity_is_idempotent(tmp_path):
    clock = Clock(1_000.0)
    d = _dir(tmp_path, clock=clock)
    first = _register(d)
    clock.t += 30
    second = _register(d, _payload(capabilities={"tunnel": True, "runner": False}))
    assert len(list((tmp_path / "hosts").glob("*.json"))) == 1
    assert second["last_seen"] == 1_030.0
    assert second["first_seen"] == first["first_seen"]
    assert second["capabilities"] == {"tunnel": True, "runner": False}
    # The refresh credential is re-published, never re-minted or appended.
    assert second["refresh"] == first["refresh"]
    assert (
        "previous_instance_id" not in second or second["previous_instance_id"] is None
    )


def test_ten_reconnects_leave_exactly_one_entry(tmp_path):
    clock = Clock()
    d = _dir(tmp_path, clock=clock)
    for _ in range(10):
        clock.t += 5
        _register(d)
    assert len(list((tmp_path / "hosts").glob("*.json"))) == 1


# --- AC4b: restart vs clone, arbitrated by probing --------------------------


def _incumbent(tmp_path, probe, clock):
    d = _dir(tmp_path, clock=clock, probe=probe)
    _register(d)
    return d


def test_a_live_incumbent_answering_with_its_own_id_refuses_the_clone(tmp_path):
    clock = Clock()
    probe = Prober({PUBLIC_URL: "inst-1"})
    d = _incumbent(tmp_path, probe, clock)
    before = (tmp_path / "hosts" / f"{_payload()['host_id']}.json").read_bytes()
    clock.t += 10
    clone = _payload(instance_id="inst-2", refresh="refresh-token-2")
    with pytest.raises(DuplicateIdentity) as e:
        _register(d, clone)
    assert probe.urls == [PUBLIC_URL]
    # The incumbent's entry is byte-identical: the clone changed nothing.
    assert (tmp_path / "hosts" / f"{_payload()['host_id']}.json").read_bytes() == before
    assert "reidentify" in str(e.value)


def test_an_unreachable_incumbent_yields_a_takeover_recording_the_predecessor(tmp_path):
    clock = Clock()
    probe = Prober(raises=TimeoutError("no route"))
    d = _incumbent(tmp_path, probe, clock)
    clock.t += 10
    entry = _register(d, _payload(instance_id="inst-2"))
    assert entry["instance_id"] == "inst-2"
    assert entry["previous_instance_id"] == "inst-1"
    assert len(list((tmp_path / "hosts").glob("*.json"))) == 1


def test_a_heartbeat_after_a_takeover_keeps_the_predecessor_on_record(tmp_path):
    clock = Clock()
    probe = Prober(raises=TimeoutError("no route"))
    d = _incumbent(tmp_path, probe, clock)
    clock.t += 10
    _register(d, _payload(instance_id="inst-2"))
    clock.t += 10
    # The takeover is a fact about the entry; the next idempotent re-post must
    # not silently erase who used to hold it.
    assert (
        _register(d, _payload(instance_id="inst-2"))["previous_instance_id"] == "inst-1"
    )


def test_a_probe_returning_no_id_yields_a_takeover(tmp_path):
    clock = Clock()
    probe = Prober({})  # answered, but not by an mship host
    d = _incumbent(tmp_path, probe, clock)
    clock.t += 10
    assert (
        _register(d, _payload(instance_id="inst-2"))["previous_instance_id"] == "inst-1"
    )


def test_a_probe_answering_with_the_claimants_id_is_a_restart_takeover(tmp_path):
    # The old tunnel is gone and sish now routes the subdomain to the new
    # process: this is a restart, not a clone.
    clock = Clock()
    probe = Prober({PUBLIC_URL: "inst-2"})
    d = _incumbent(tmp_path, probe, clock)
    clock.t += 10
    entry = _register(d, _payload(instance_id="inst-2"))
    assert entry["instance_id"] == "inst-2"
    assert entry["previous_instance_id"] == "inst-1"


def test_a_different_machine_fingerprint_against_a_live_incumbent_is_refused(tmp_path):
    clock = Clock()
    probe = Prober({PUBLIC_URL: "inst-1"})
    d = _incumbent(tmp_path, probe, clock)
    clock.t += 10
    with pytest.raises(DuplicateIdentity):
        _register(
            d, _payload(instance_id="inst-3", machine_fingerprint="other-machine")
        )


def test_a_stale_entry_is_taken_over_without_probing(tmp_path):
    clock = Clock()
    probe = Prober({PUBLIC_URL: "inst-1"})
    d = _incumbent(tmp_path, probe, clock)
    probe.urls.clear()
    clock.t += host_contract.DIRECTORY_STALE_S + 1
    entry = _register(d, _payload(instance_id="inst-2"))
    assert entry["previous_instance_id"] == "inst-1"
    assert probe.urls == [], "a stale incumbent needs no probe"


def test_a_stale_host_id_cannot_be_claimed_by_another_approved_key(tmp_path):
    clock = Clock()
    probe = Prober()
    d = _dir(tmp_path, clock=clock, probe=probe, signers=(FP_A, FP_B))
    _register(d)
    path = tmp_path / "hosts" / f"{_payload()['host_id']}.json"
    before = path.read_bytes()
    clock.t += host_contract.DIRECTORY_STALE_S + 1

    claimant = _payload(instance_id="inst-2", key_fingerprint=FP_B)
    with pytest.raises(DuplicateIdentity):
        _register(d, claimant, fingerprint=FP_B)

    assert path.read_bytes() == before
    assert probe.urls == []


# --- listing ----------------------------------------------------------------


def test_list_hosts_marks_stale_entries_offline_and_merges_pending(tmp_path):
    clock = Clock()
    d = _dir(tmp_path, clock=clock)
    _register(d)
    pending = [
        {
            "id": "abc",
            "hostname": "vm-beta",
            "fingerprint": FP_B,
            "created_at": clock.t,
            "status": "pending",
        }
    ]
    listed = d.list_hosts(pending=pending)
    assert [h["state"] for h in listed] == ["online", "pending-approval"]
    assert listed[1]["label"] == "vm-beta"
    assert listed[1]["key_fingerprint"] == FP_B
    clock.t += host_contract.DIRECTORY_STALE_S + 1
    assert d.list_hosts()[0]["state"] == "offline"


def test_a_pending_request_for_an_already_registered_key_is_not_listed_twice(tmp_path):
    d = _dir(tmp_path)
    _register(d)
    pending = [
        {
            "id": "abc",
            "hostname": "vm-alpha",
            "fingerprint": FP_A,
            "created_at": 0.0,
            "status": "pending",
        }
    ]
    assert [h["state"] for h in d.list_hosts(pending=pending)] == ["online"]


def test_two_hosts_stay_independent(tmp_path):
    clock = Clock()
    d = _dir(tmp_path, clock=clock, signers=(FP_A, FP_B))
    _register(d)
    beta = _payload(
        host_id="hst-20260818120000-bbbbbbbb",
        instance_id="inst-b",
        key_fingerprint=FP_B,
        machine_fingerprint="machine-b",
        subdomain="def456-d4e5f6",
        public_url="https://def456-d4e5f6.relay.example",
        refresh="refresh-b",
    )
    _register(d, beta, fingerprint=FP_B)
    clock.t += host_contract.DIRECTORY_STALE_S + 1
    # Alpha goes stale; beta re-registers and is unaffected by its neighbour.
    _register(d, beta, fingerprint=FP_B)
    states = {h["host_id"]: h["state"] for h in d.list_hosts()}
    assert states == {
        _payload()["host_id"]: "offline",
        beta["host_id"]: "online",
    }
    subdomains = {h["subdomain"] for h in d.list_hosts()}
    assert subdomains == {SUBDOMAIN, "def456-d4e5f6"}


def test_a_corrupt_entry_file_is_quarantined_not_fatal(tmp_path):
    d = _dir(tmp_path)
    _register(d)
    (tmp_path / "hosts" / "broken.json").write_text("{not json")
    assert [h["host_id"] for h in d.list_hosts()] == [_payload()["host_id"]]
    assert (tmp_path / "hosts" / "broken.json.corrupt").is_file()
    assert not (tmp_path / "hosts" / "broken.json").exists()


def test_quarantine_cannot_move_a_concurrently_repaired_host_record(
    tmp_path, monkeypatch
):
    d = _dir(tmp_path)
    host_id = _payload()["host_id"]
    path = tmp_path / "hosts" / f"{host_id}.json"
    path.write_text("{not json")
    corrupt_read = threading.Event()
    writer_finished = threading.Event()
    read_text = Path.read_text

    def pause_after_corrupt_read(self, *args, **kwargs):
        raw = read_text(self, *args, **kwargs)
        if self == path and raw == "{not json" and not corrupt_read.is_set():
            corrupt_read.set()
            writer_finished.wait(timeout=0.1)
        return raw

    monkeypatch.setattr(Path, "read_text", pause_after_corrupt_read)

    def register():
        try:
            return _register(d)
        finally:
            writer_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(d.get_host, host_id)
        assert corrupt_read.wait(timeout=1)
        writer = executor.submit(register)
        reader.result()
        assert writer.result()["host_id"] == host_id

    assert d.get_host(host_id)["instance_id"] == "inst-1"


@pytest.mark.parametrize("bad", ["yesterday", None, "NaN", "Infinity", "-Infinity"])
def test_an_unusable_last_seen_reads_as_offline_not_as_a_crash(tmp_path, bad):
    # A hand-edited entry must degrade, not brick every listing — and must not
    # read as FRESH, which would make it permanently online and un-takeoverable
    # (`now - nan >= stale` is False, so NaN needs the same treatment as junk).
    clock = Clock()
    d = _dir(tmp_path, clock=clock)
    _register(d)
    path = tmp_path / "hosts" / f"{_payload()['host_id']}.json"
    rec = json.loads(path.read_text())
    rec["last_seen"] = bad
    path.write_text(json.dumps(rec))
    assert d.list_hosts()[0]["state"] == "offline"
    assert (
        _register(d, _payload(instance_id="inst-2"))["previous_instance_id"] == "inst-1"
    )


def test_the_directory_survives_a_process_restart(tmp_path):
    # On-disk store (AC3): a fresh HostDirectory over the same dir sees it.
    clock = Clock()
    _register(_dir(tmp_path, clock=clock))
    assert (
        _dir(tmp_path, clock=clock).get_host(_payload()["host_id"])["label"]
        == "vm-alpha"
    )


@pytest.mark.parametrize(
    "public_url",
    (
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8080",
        "https://foreign.example",
    ),
)
def test_a_signed_registration_cannot_publish_a_non_relay_url(tmp_path, public_url):
    d = _dir(tmp_path)

    with pytest.raises(ValueError):
        _register(d, _payload(public_url=public_url))

    assert d.get_host(_payload()["host_id"]) is None


@pytest.mark.parametrize("subdomain", ("enroll", "egress"))
def test_a_host_cannot_claim_a_reserved_relay_route(tmp_path, subdomain):
    d = _dir(tmp_path)

    with pytest.raises(ValueError):
        _register(
            d,
            _payload(
                subdomain=subdomain,
                public_url=f"https://{subdomain}.relay.example",
            ),
        )

    assert d.get_host(_payload()["host_id"]) is None


def test_instance_probe_never_follows_a_relay_redirect():
    calls = []

    class Redirect:
        status_code = 302

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Redirect()

    assert probe_instance_id(PUBLIC_URL, get=get) is None
    assert calls == [
        (
            f"{PUBLIC_URL}/health",
            {"timeout": 5.0, "follow_redirects": False},
        )
    ]


def test_concurrent_requests_for_one_identity_share_one_live_challenge(tmp_path):
    d = _dir(tmp_path)
    barrier = threading.Barrier(2)
    sweep = d._sweep_challenges

    def racing_sweep():
        live = sweep()
        try:
            barrier.wait(timeout=0.1)
        except threading.BrokenBarrierError:
            pass
        return live

    d._sweep_challenges = racing_sweep
    with ThreadPoolExecutor(max_workers=2) as executor:
        challenges = list(executor.map(lambda _: d.issue_challenge(FP_A), range(2)))

    assert challenges[0] == challenges[1]
    assert len(list((tmp_path / "challenges").glob("*.json"))) == 1


def test_same_host_registration_arbitration_is_serialized(tmp_path):
    d = _dir(tmp_path, signers=(FP_A, FP_B))
    _register(d)
    entry_path = tmp_path / "hosts" / f"{_payload()['host_id']}.json"
    barrier = threading.Barrier(2)

    def probe(_public_url):
        incumbent_id = json.loads(entry_path.read_text())["instance_id"]
        try:
            barrier.wait(timeout=0.1)
        except threading.BrokenBarrierError:
            pass
        return None if incumbent_id == "inst-1" else incumbent_id

    d._probe = probe
    payloads = [
        _payload(instance_id="inst-2", key_fingerprint=FP_A),
        _payload(instance_id="inst-3", key_fingerprint=FP_B),
    ]
    nonces = [
        d.issue_challenge(str(payload["key_fingerprint"]))["nonce"]
        for payload in payloads
    ]

    def register(index):
        payload = payloads[index]
        try:
            d.register(
                payload,
                nonce=nonces[index],
                signature=_sign(
                    nonces[index], payload, str(payload["key_fingerprint"])
                ),
            )
            return "registered"
        except DuplicateIdentity:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(register, range(2)))

    assert sorted(outcomes) == ["duplicate", "registered"]
    assert d.get_host(_payload()["host_id"])["instance_id"] in {"inst-2", "inst-3"}
