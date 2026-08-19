import json
import logging

import pytest
from fastapi.testclient import TestClient
from mship.core.relay import host_contract
from mship.core.relay.enroll import RequestStore
from mship.core.relay.enroll_app import build_enroll_app
from mship.core.relay.fleet_token import FleetTokenStore
from mship.core.relay.host_directory import HostDirectory, VerificationBusy

_PUB = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAAAAAA host"
)
_PUB_B = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISecondKeyBodyBBBBBBBBBBBBBBBBBBBBBBBB two"
)
RELAY = "mship-relay.atomikpanda.com"

FP_A = "SHA256:keyA"
MACHINE = "machine-fingerprint-copied-by-cp-a"


class Harness:
    """One enroll app plus the stores behind it, so a test can assert on both
    the HTTP surface and what actually landed on disk."""

    def __init__(self, client, store, directory, fleet, clock, prober, revoked):
        self.client = client
        self.store = store
        self.directory = directory
        self.fleet = fleet
        self.clock = clock
        self.prober = prober
        self.revoked = revoked


class Clock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Prober:
    """Fake arbitration probe (the `test_host_directory.py` shape)."""

    def __init__(self, answers=None):
        self.urls: list[str] = []
        self._answers = answers or {}

    def __call__(self, public_url):
        self.urls.append(public_url)
        return self._answers.get(public_url)


def _verify(blob, *, signature, identity, allowed_signers, namespace):
    """Stand-in for `ssh_sig.verify_blob`: `sig:<fp>:<blob>` from an allowlisted key."""
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
        "subdomain": "abc123-a1b2c3",
        "public_url": f"https://abc123-a1b2c3.{RELAY}",
        "mship_version": "1.2.3",
        "capabilities": {"tunnel": True},
        "runner": {"enabled": False, "state": "disabled"},
        "refresh": "refresh-credential-1",
    }
    payload.update(over)
    return payload


def _harness(tmp_path, cap=50, answers=None, ttl=host_contract.ENROLL_TTL_S):
    base = tmp_path / "s"
    store = RequestStore(base, ttl_seconds=ttl, max_pending=cap)
    clock = Clock()
    prober = Prober(answers)
    revoked = []
    directory = HostDirectory(
        base,
        relay_domain=RELAY,
        allowed_signers=lambda: FP_A,
        probe=prober,
        revoke_signer=lambda identity: revoked.append(identity),
        verify=_verify,
        clock=clock,
    )
    fleet = FleetTokenStore(base)
    app = build_enroll_app(
        store, relay_domain=RELAY, host_directory=directory, fleet_tokens=fleet
    )
    return Harness(
        TestClient(app), store, directory, fleet, clock, prober, revoked
    )


def _challenge(h, identity=FP_A):
    return h.client.post(
        host_contract.CHALLENGE_PATH, json={"key_fingerprint": identity}
    )


def _register(h, payload=None, nonce=None, signature=None):
    payload = _payload() if payload is None else payload
    if nonce is None:
        nonce = _challenge(h, str(payload["key_fingerprint"])).json()["nonce"]
    return h.client.post(
        host_contract.REGISTER_PATH,
        json={
            "nonce": nonce,
            "signature": _sign(nonce, payload) if signature is None else signature,
            "payload": payload,
        },
    )


def _client(tmp_path, cap=50, ttl=host_contract.ENROLL_TTL_S):
    h = _harness(tmp_path, cap=cap, ttl=ttl)
    return h.client, h.store


def _ask_client(tmp_path):
    return _harness(tmp_path).client


def test_enroll_creates_pending_with_the_store_ttl(tmp_path):
    c, _ = _client(tmp_path, ttl=60)
    r = c.post("/enroll", json={"pubkey": _PUB, "hostname": "laptop"})
    assert r.status_code == 200
    assert r.json()["expires_in"] == 60
    rid = r.json()["id"]
    assert c.get(f"/status/{rid}").json()["status"] == "pending"


def test_enroll_repost_observes_an_already_approved_request(tmp_path):
    client, store = _client(tmp_path)
    first = client.post("/enroll", json={"pubkey": _PUB, "hostname": "laptop"})
    rid = first.json()["id"]
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()
    store.approve(rid, pubkeys)

    repost = client.post("/enroll", json={"pubkey": _PUB, "hostname": "laptop"})

    assert repost.json() == {
        "id": rid,
        "status": "approved",
        "expires_in": host_contract.ENROLL_TTL_S,
    }
    assert store.list_pending() == []


def test_enroll_rejects_bad_key(tmp_path):
    c, _ = _client(tmp_path)
    assert (
        c.post("/enroll", json={"pubkey": "garbage", "hostname": "x"}).status_code
        == 400
    )


def test_enroll_over_cap_429(tmp_path):
    # Distinct keys: same-key re-posts are deduped, so only distinct devices
    # can reach the cap.
    c, _ = _client(tmp_path, cap=1)
    c.post("/enroll", json={"pubkey": _PUB, "hostname": "a"})
    assert (
        c.post("/enroll", json={"pubkey": _PUB_B, "hostname": "b"}).status_code == 429
    )


def test_status_unknown(tmp_path):
    c, _ = _client(tmp_path)
    assert c.get("/status/deadbeef").json()["status"] == "unknown"


def test_enroll_rejects_oversized_pubkey(tmp_path):
    c, _ = _client(tmp_path)
    # A valid key-type prefix + an absurdly long body: the body bound must reject
    # this before we read+hash+store it. 4xx (pydantic 422 or our 400), never 2xx/5xx.
    oversized = "ssh-ed25519 " + "A" * 4096 + " host"
    r = c.post("/enroll", json={"pubkey": oversized, "hostname": "x"})
    assert 400 <= r.status_code < 500


def test_tls_check_allows_relay_owned_host(tmp_path):
    c = _ask_client(tmp_path)
    assert c.get("/tls-check", params={"domain": f"enroll.{RELAY}"}).status_code == 200
    assert (
        c.get("/tls-check", params={"domain": f"w-92bbb7.{RELAY}"}).status_code == 200
    )


def test_tls_check_rejects_foreign_host(tmp_path):
    c = _ask_client(tmp_path)
    assert c.get("/tls-check", params={"domain": "evil.com"}).status_code == 403


def test_tls_check_requires_domain(tmp_path):
    c = _ask_client(tmp_path)
    assert c.get("/tls-check").status_code in (400, 422)


# ---------------------------------------------------------------------------
# /hosts/challenge
# ---------------------------------------------------------------------------


def test_challenge_issues_a_nonce(tmp_path):
    h = _harness(tmp_path)
    r = _challenge(h)
    assert r.status_code == 200
    body = r.json()
    assert body["nonce"] and isinstance(body["nonce"], str)
    assert body["expires_at"] == h.clock.t + host_contract.CHALLENGE_TTL_S


def test_challenge_is_reused_for_the_same_approved_identity(tmp_path):
    h = _harness(tmp_path)
    assert _challenge(h).json()["nonce"] == _challenge(h).json()["nonce"]


def test_unapproved_identity_cannot_allocate_challenge_storage(tmp_path):
    h = _harness(tmp_path)

    r = _challenge(h, "SHA256:not-approved")

    assert r.status_code == 401
    assert r.json()["detail"] == host_contract.UNAPPROVED_KEY_DETAIL
    assert list((tmp_path / "s" / "challenges").glob("*.json")) == []


def test_signed_key_revocation_removes_the_current_relay_signer(tmp_path):
    h = _harness(tmp_path)
    nonce = _challenge(h).json()["nonce"]
    payload = host_contract.key_revocation_payload(FP_A)

    response = h.client.post(
        host_contract.REVOKE_PATH,
        json={
            "key_fingerprint": FP_A,
            "nonce": nonce,
            "signature": _sign(nonce, payload),
        },
    )

    assert response.status_code == 200
    assert h.revoked == [FP_A]



def test_key_revocation_rejects_a_signature_not_made_by_that_key(tmp_path):
    h = _harness(tmp_path)
    nonce = _challenge(h).json()["nonce"]

    response = h.client.post(
        host_contract.REVOKE_PATH,
        json={
            "key_fingerprint": FP_A,
            "nonce": nonce,
            "signature": "not-a-valid-signature",
        },
    )

    assert response.status_code == 401
    assert h.revoked == []

# ---------------------------------------------------------------------------
# /hosts/register
# ---------------------------------------------------------------------------


def test_register_with_a_valid_signature_lands_the_entry(tmp_path):
    h = _harness(tmp_path)
    r = _register(h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "registered"
    entry = h.directory.get_host(_payload()["host_id"])
    assert entry is not None
    assert entry["instance_id"] == "inst-1"
    assert entry["last_seen"] == h.clock.t  # relay clock, not the payload's


def test_signed_non_relay_url_is_a_clean_400(tmp_path):
    h = _harness(tmp_path)
    payload = _payload(public_url="http://169.254.169.254/latest/meta-data")

    response = _register(h, payload)

    assert response.status_code == 400
    assert h.directory.get_host(payload["host_id"]) is None


def test_register_never_echoes_the_refresh_credential(tmp_path):
    # `GET /hosts` publishes it to the fleet-token holder BY DESIGN; no other
    # route may widen it — least of all this unauthenticated one.
    h = _harness(tmp_path)
    assert "refresh-credential-1" not in _register(h).text


def test_challenge_cannot_authorize_a_different_identity(tmp_path):
    h = _harness(tmp_path)
    nonce = _challenge(h).json()["nonce"]
    payload = _payload(key_fingerprint="SHA256:notApproved")
    r = _register(
        h,
        payload,
        nonce=nonce,
        signature=_sign(nonce, payload, "SHA256:notApproved"),
    )
    assert r.status_code == 401
    assert h.directory.get_host(payload["host_id"]) is None


def test_register_with_a_forged_signature_is_401(tmp_path):
    h = _harness(tmp_path)
    r = _register(h, signature="sig:SHA256:keyA:not-the-blob")
    assert r.status_code == 401
    assert h.directory.get_host(_payload()["host_id"]) is None


def test_register_verification_capacity_is_429(tmp_path):
    h = _harness(tmp_path)

    def busy(*args, **kwargs):
        raise VerificationBusy

    h.directory.register = busy

    response = _register(h)

    assert response.status_code == 429
    assert response.json()["detail"] == "signature verification busy; try later"
    assert response.headers["retry-after"] == "1"


def test_register_with_an_unknown_nonce_is_401(tmp_path):
    h = _harness(tmp_path)
    payload = _payload()
    assert _register(h, payload, nonce="deadbeef").status_code == 401


def test_register_with_a_traversal_shaped_host_id_is_400(tmp_path):
    h = _harness(tmp_path)
    assert _register(h, _payload(host_id="../../etc/passwd")).status_code == 400


def test_duplicate_identity_is_409_naming_the_recovery_command(tmp_path):
    # The clone case: a live incumbent that answers as itself refuses the claim,
    # and the operator's only recovery must be named in the refusal.
    h = _harness(tmp_path, answers={f"https://abc123-a1b2c3.{RELAY}": "inst-1"})
    assert _register(h).status_code == 200
    r = _register(h, _payload(instance_id="inst-2"))
    assert r.status_code == 409
    assert "mship daemon reidentify" in r.json()["detail"]


def test_one_approved_key_cannot_register_a_second_host_id(tmp_path):
    h = _harness(tmp_path)
    assert _register(h).status_code == 200
    second = _payload(
        host_id="hst-20260818120000-bbbbbbbb",
        instance_id="inst-2",
        subdomain="def456-d4e5f6",
        public_url=f"https://def456-d4e5f6.{RELAY}",
    )

    response = _register(h, second)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "approved key is already bound to another host_id; "
        "run `mship daemon reidentify` to rotate the key"
    )


def _signed_body(h, **payload_over):
    """A body that would REGISTER (fresh nonce, matching signature) — so a case
    below can 422 for exactly one reason: the bound it oversizes."""
    payload = _payload(**payload_over)
    nonce = _challenge(h, str(payload["key_fingerprint"])).json()["nonce"]
    return {"nonce": nonce, "signature": _sign(nonce, payload), "payload": payload}


def _oversized_nonce(h):
    body = _signed_body(h)
    body["nonce"] = "n" * 200
    return body


def _oversized_signature(h):
    body = _signed_body(h)
    body["signature"] = "A" * 9_000
    return body


_BOUNDED_BODIES = {
    "nonce": _oversized_nonce,
    "signature": _oversized_signature,
    "payload-value": lambda h: _signed_body(h, label="w" * 1_000),
    "payload-key": lambda h: _signed_body(h, **{"k" * 300: "v"}),
    "payload-field-count": lambda h: _signed_body(
        h, **{f"x{i}": "v" for i in range(80)}
    ),
    "nested-key": lambda h: _signed_body(h, capabilities={"k" * 300: True}),
    "nested-value": lambda h: _signed_body(h, capabilities={"tunnel": "w" * 1_000}),
    "nested-field-count": lambda h: _signed_body(
        h, capabilities={f"c{i}": True for i in range(40)}
    ),
}


def test_the_bounds_control_body_is_otherwise_accepted(tmp_path):
    # Without this, every case below could be 422ing for an unrelated reason and
    # the bounds could be deleted with the suite still green.
    h = _harness(tmp_path)
    assert (
        h.client.post(host_contract.REGISTER_PATH, json=_signed_body(h)).status_code
        == 200
    )


@pytest.mark.parametrize("case", sorted(_BOUNDED_BODIES))
def test_register_bounds_every_body_field(tmp_path, case):
    # 422 exactly: pydantic validation is the ONLY thing that may reject these.
    # Drop the bound and the request is accepted (200) or refused by route logic
    # (401) — either way this fails, which is what makes the assertion real.
    h = _harness(tmp_path)
    r = h.client.post(host_contract.REGISTER_PATH, json=_BOUNDED_BODIES[case](h))
    assert r.status_code == 422, (case, r.status_code, r.text[:200])


@pytest.mark.parametrize(
    "payload",
    [
        _payload(clock_skew_seconds=float("nan")),
        _payload(runner={"load": float("inf")}),
    ],
)
def test_register_rejects_non_finite_scalars_at_the_request_boundary(tmp_path, payload):
    h = _harness(tmp_path)
    client = TestClient(h.client.app, raise_server_exceptions=False)
    nonce = _challenge(h).json()["nonce"]
    body = json.dumps(
        {"nonce": nonce, "signature": "s", "payload": payload},
        allow_nan=True,
    )

    response = client.post(
        host_contract.REGISTER_PATH,
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET /hosts
# ---------------------------------------------------------------------------


def test_directory_requires_a_fleet_token(tmp_path):
    h = _harness(tmp_path)
    assert h.client.get(host_contract.LIST_PATH).status_code == 401


def test_directory_refuses_a_revoked_fleet_token(tmp_path):
    h = _harness(tmp_path)
    token = h.fleet.issue("phone")
    h.fleet.revoke("phone")
    r = h.client.get(
        host_contract.LIST_PATH, headers={host_contract.FLEET_TOKEN_HEADER: token}
    )
    assert r.status_code == 401


def test_directory_lists_registered_hosts_and_pending_enrollments(tmp_path):
    h = _harness(tmp_path)
    _register(h)
    h.client.post("/enroll", json={"pubkey": _PUB_B, "hostname": "fresh-vm"})
    r = h.client.get(
        host_contract.LIST_PATH,
        headers={host_contract.FLEET_TOKEN_HEADER: h.fleet.issue("phone")},
    )
    assert r.status_code == 200
    hosts = r.json()["hosts"]
    states = {e["state"] for e in hosts}
    assert states == {"online", "pending-approval"}
    registered = next(e for e in hosts if e["state"] == "online")
    # The phone fetches the refresh credential from here — that IS the design.
    assert registered["refresh"] == "refresh-credential-1"
    assert registered["host_id"] == _payload()["host_id"]
    assert (
        next(e for e in hosts if e["state"] == "pending-approval")["label"]
        == "fresh-vm"
    )


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("last_seen", 10**400),
        ("first_seen", float("nan")),
    ],
)
def test_directory_degrades_unusable_persisted_timestamps(
    tmp_path, field, bad_value
):
    h = _harness(tmp_path)
    _register(h)
    path = tmp_path / "s" / "hosts" / f"{_payload()['host_id']}.json"
    rec = json.loads(path.read_text())
    rec[field] = bad_value
    path.write_text(json.dumps(rec, allow_nan=True))

    response = h.client.get(
        host_contract.LIST_PATH,
        headers={host_contract.FLEET_TOKEN_HEADER: h.fleet.issue("phone")},
    )

    assert response.status_code == 200
    host = response.json()["hosts"][0]
    assert host[field] == 0.0
    if field == "last_seen":
        assert host["state"] == "offline"


def test_directory_publishes_a_deliberate_projection_not_the_raw_record(tmp_path):
    # A field added to a stored entry must not auto-publish to every paired
    # phone; the response shape is an allowlist, asserted here.
    h = _harness(tmp_path)
    _register(h)
    path = tmp_path / "s" / "hosts" / f"{_payload()['host_id']}.json"
    rec = json.loads(path.read_text())
    rec["operator_note"] = "not for the wire"
    path.write_text(json.dumps(rec))
    r = h.client.get(
        host_contract.LIST_PATH,
        headers={host_contract.FLEET_TOKEN_HEADER: h.fleet.issue("phone")},
    )
    assert "operator_note" not in r.json()["hosts"][0]


def test_the_fleet_token_is_never_logged(tmp_path, caplog):
    h = _harness(tmp_path)
    _register(h)
    token = h.fleet.issue("phone")
    with caplog.at_level(logging.DEBUG):
        h.client.get(
            host_contract.LIST_PATH, headers={host_contract.FLEET_TOKEN_HEADER: token}
        )
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert token not in logged
    assert "refresh-credential-1" not in logged


def test_the_parsed_payload_is_byte_identical_to_what_was_signed(tmp_path):
    # The catastrophic silent failure: if the body model coerced a value or
    # filled in a default the client never sent, `canonical_payload` would
    # differ and EVERY registration would 401 in production while unit tests
    # over the directory alone stayed green.
    from mship.core.relay.enroll_app import _RegisterBody

    sent = _payload(
        mship_version="1.2.3",
        capabilities={"tunnel": True, "runner": False},
        label="wörk-hôst",
    )
    parsed = _RegisterBody(nonce="n", signature="s", payload=sent).payload
    assert host_contract.canonical_payload(parsed) == host_contract.canonical_payload(
        sent
    )


def test_a_future_daemon_field_survives_the_body_model(tmp_path):
    # The payload is signed as sent, so the app must not drop unknown keys —
    # dropping one would break the signature rather than merely ignore it.
    h = _harness(tmp_path)
    payload = _payload(some_future_field="v")
    assert _register(h, payload).status_code == 200
