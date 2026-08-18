"""The relay host directory: the relay never asserts identity — every entry it
writes is backed by a signature it verified against the same `pubkeys/`
allowlist sish authenticates against, every freshness decision is stamped by
the relay's own clock, and a second live claimant is arbitrated by probing the
incumbent rather than by trusting a copyable fingerprint."""
from __future__ import annotations

import json

import pytest

from mship.core.relay import host_contract
from mship.core.relay.host_directory import (
    ChallengeRefused,
    DuplicateIdentity,
    HostDirectory,
    InvalidHostId,
    SignatureRefused,
)

FP_A = "SHA256:keyA"
FP_B = "SHA256:keyB"
MACHINE = "machine-fingerprint-copied-by-cp-a"


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
        "subdomain": "abc123",
        "public_url": "https://abc123.relay.example",
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
        allowed_signers=lambda: "\n".join(signers),
        verify=_verify,
        probe=probe if probe is not None else Prober(),
        clock=clock or Clock(),
    )


def _register(d, payload=None, *, fingerprint=FP_A, signature=None):
    payload = payload if payload is not None else _payload()
    nonce = d.issue_challenge()["nonce"]
    sig = signature if signature is not None else _sign(nonce, payload, fingerprint)
    return d.register(payload, nonce=nonce, signature=sig)


# --- challenges -------------------------------------------------------------


def test_a_nonce_is_single_use_and_relay_stamped(tmp_path):
    clock = Clock(5_000.0)
    d = _dir(tmp_path, clock=clock)
    ch = d.issue_challenge()
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
    ch = d.issue_challenge()
    clock.t += host_contract.CHALLENGE_TTL_S + 1
    payload = _payload()
    with pytest.raises(ChallengeRefused):
        d.register(payload, nonce=ch["nonce"], signature=_sign(ch["nonce"], payload))
    assert d.get_host(payload["host_id"]) is None


def test_an_unknown_nonce_is_refused(tmp_path):
    d = _dir(tmp_path)
    payload = _payload()
    with pytest.raises(ChallengeRefused):
        d.register(payload, nonce="never-issued", signature=_sign("never-issued", payload))


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
    assert entry["public_url"] == "https://abc123.relay.example"
    assert entry["last_seen"] == 9_000.0 and entry["first_seen"] == 9_000.0
    assert list((tmp_path / "hosts").glob("*.json")) != []


def test_the_store_is_owner_only_because_an_entry_carries_a_refresh_credential(tmp_path):
    d = _dir(tmp_path)
    _register(d)
    assert oct((tmp_path / "hosts").stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "challenges").stat().st_mode & 0o777) == "0o700"
    entry = tmp_path / "hosts" / f"{_payload()['host_id']}.json"
    assert oct(entry.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    "kind", ["unsigned", "wrong-key", "tampered-payload", "not-allowlisted"]
)
def test_no_entry_is_ever_written_without_a_verified_signature(tmp_path, kind):
    d = _dir(tmp_path)
    payload = _payload()
    nonce = d.issue_challenge()["nonce"]
    if kind == "unsigned":
        sig = ""
    elif kind == "wrong-key":
        # Signed by an allowlisted key that is NOT the one the payload claims.
        sig = _sign(nonce, payload, fingerprint=FP_B)
    elif kind == "tampered-payload":
        sig = _sign(nonce, _payload(public_url="https://evil.example"))
    else:
        payload = _payload(key_fingerprint=FP_B)
        sig = _sign(nonce, payload, fingerprint=FP_B)
    with pytest.raises(SignatureRefused):
        d.register(payload, nonce=nonce, signature=sig)
    assert d.get_host(payload["host_id"]) is None
    assert list((tmp_path / "hosts").glob("*.json")) == []


def test_a_traversal_shaped_host_id_never_touches_the_filesystem(tmp_path):
    d = _dir(tmp_path)
    payload = _payload(host_id="../../evil")
    nonce = d.issue_challenge()["nonce"]
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
    assert "previous_instance_id" not in second or second["previous_instance_id"] is None


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
    probe = Prober({"https://abc123.relay.example": "inst-1"})
    d = _incumbent(tmp_path, probe, clock)
    before = (tmp_path / "hosts" / f"{_payload()['host_id']}.json").read_bytes()
    clock.t += 10
    clone = _payload(instance_id="inst-2", refresh="refresh-token-2",
                     public_url="https://clone.relay.example")
    with pytest.raises(DuplicateIdentity) as e:
        _register(d, clone)
    assert probe.urls == ["https://abc123.relay.example"]
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


def test_a_probe_returning_no_id_yields_a_takeover(tmp_path):
    clock = Clock()
    probe = Prober({})            # answered, but not by an mship host
    d = _incumbent(tmp_path, probe, clock)
    clock.t += 10
    assert _register(d, _payload(instance_id="inst-2"))["previous_instance_id"] == "inst-1"


def test_a_probe_answering_with_the_claimants_id_is_a_restart_takeover(tmp_path):
    # The old tunnel is gone and sish now routes the subdomain to the new
    # process: this is a restart, not a clone.
    clock = Clock()
    probe = Prober({"https://abc123.relay.example": "inst-2"})
    d = _incumbent(tmp_path, probe, clock)
    clock.t += 10
    entry = _register(d, _payload(instance_id="inst-2"))
    assert entry["instance_id"] == "inst-2"
    assert entry["previous_instance_id"] == "inst-1"


def test_a_different_machine_fingerprint_against_a_live_incumbent_is_refused(tmp_path):
    clock = Clock()
    probe = Prober({"https://abc123.relay.example": "inst-1"})
    d = _incumbent(tmp_path, probe, clock)
    clock.t += 10
    with pytest.raises(DuplicateIdentity):
        _register(d, _payload(instance_id="inst-3", machine_fingerprint="other-machine"))


def test_a_stale_entry_is_taken_over_without_probing(tmp_path):
    clock = Clock()
    probe = Prober({"https://abc123.relay.example": "inst-1"})
    d = _incumbent(tmp_path, probe, clock)
    probe.urls.clear()
    clock.t += host_contract.DIRECTORY_STALE_S + 1
    entry = _register(d, _payload(instance_id="inst-2"))
    assert entry["previous_instance_id"] == "inst-1"
    assert probe.urls == [], "a stale incumbent needs no probe"


# --- listing ----------------------------------------------------------------


def test_list_hosts_marks_stale_entries_offline_and_merges_pending(tmp_path):
    clock = Clock()
    d = _dir(tmp_path, clock=clock)
    _register(d)
    pending = [{
        "id": "abc", "hostname": "vm-beta", "fingerprint": FP_B,
        "created_at": clock.t, "status": "pending",
    }]
    listed = d.list_hosts(pending=pending)
    assert [h["state"] for h in listed] == ["online", "pending-approval"]
    assert listed[1]["label"] == "vm-beta"
    assert listed[1]["key_fingerprint"] == FP_B
    clock.t += host_contract.DIRECTORY_STALE_S + 1
    assert d.list_hosts()[0]["state"] == "offline"


def test_a_pending_request_for_an_already_registered_key_is_not_listed_twice(tmp_path):
    d = _dir(tmp_path)
    _register(d)
    pending = [{"id": "abc", "hostname": "vm-alpha", "fingerprint": FP_A,
                "created_at": 0.0, "status": "pending"}]
    assert [h["state"] for h in d.list_hosts(pending=pending)] == ["online"]


def test_two_hosts_stay_independent(tmp_path):
    clock = Clock()
    d = _dir(tmp_path, clock=clock, signers=(FP_A, FP_B))
    _register(d)
    beta = _payload(host_id="hst-20260818120000-bbbbbbbb", instance_id="inst-b",
                    key_fingerprint=FP_B, machine_fingerprint="machine-b",
                    subdomain="def456", public_url="https://def456.relay.example",
                    refresh="refresh-b")
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
    assert subdomains == {"abc123", "def456"}


def test_a_corrupt_entry_file_is_quarantined_not_fatal(tmp_path):
    d = _dir(tmp_path)
    _register(d)
    (tmp_path / "hosts" / "broken.json").write_text("{not json")
    assert [h["host_id"] for h in d.list_hosts()] == [_payload()["host_id"]]
    assert (tmp_path / "hosts" / "broken.json.corrupt").is_file()
    assert not (tmp_path / "hosts" / "broken.json").exists()


def test_the_directory_survives_a_process_restart(tmp_path):
    # On-disk store (AC3): a fresh HostDirectory over the same dir sees it.
    clock = Clock()
    _register(_dir(tmp_path, clock=clock))
    assert _dir(tmp_path, clock=clock).get_host(_payload()["host_id"])["label"] == "vm-alpha"
