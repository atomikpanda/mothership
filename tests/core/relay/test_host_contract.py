"""The host-registration wire contract is a single source (test_contract.py
style): both ends read the same objects, so they cannot drift — and the bytes
that get signed are pinned by a golden case, because key order, separators and
escaping are otherwise free parameters that would ship as "every registration
401s in production" while unit tests pass in one interpreter."""
from __future__ import annotations

import json

import pytest

from mship.core.daemon import host_token
from mship.core.relay import enroll, host_contract, tunnel


# --- identity: one owner per constant ---------------------------------------


def test_host_token_ttl_has_exactly_one_owner():
    # Same object, not a copy: the host mints on this TTL and the contract
    # publishes it, so a change is impossible to make in only one place.
    assert host_token.HOST_TOKEN_TTL_S is host_contract.HOST_TOKEN_TTL_S


def test_enroll_store_ttl_is_the_contract_ttl():
    # The daemon's re-post schedule is derived from the store's TTL; if the
    # store had its own copy the schedule could silently outlive it.
    import inspect

    ttl = inspect.signature(enroll.RequestStore).parameters["ttl_seconds"]
    assert ttl.default is host_contract.ENROLL_TTL_S


def test_enroll_repost_interval_keeps_a_pending_request_alive():
    # Re-posting must happen strictly more often than the store expires, or an
    # overnight provision is unapprovable in the morning (AC1/AC8).
    assert host_contract.ENROLL_REPOST_INTERVAL_S < host_contract.ENROLL_TTL_S


def test_directory_staleness_is_derived_so_healthy_hosts_cannot_flap():
    assert host_contract.DIRECTORY_STALE_S == (
        3 * host_contract.REGISTER_INTERVAL_S + host_contract.MAX_BACKOFF_S
    )
    # A host that misses a beat and reconnects on the worst-case backoff is
    # still inside the window (AC10: no flap to `offline`).
    assert (
        host_contract.REGISTER_INTERVAL_S + host_contract.MAX_BACKOFF_S
        < host_contract.DIRECTORY_STALE_S
    )


def test_max_backoff_matches_the_tunnel_supervisor_cap():
    import inspect

    cap = inspect.signature(tunnel.TunnelSupervisor).parameters["max_backoff_delay"]
    assert cap.default == host_contract.MAX_BACKOFF_S


# --- AC13: one Caddy matcher is enough --------------------------------------


def test_the_wire_literals_are_pinned_by_value():
    # Symbolic comparisons alone would let a rename sail through green tests
    # and break every deployed daemon: these strings ARE the wire.
    assert host_contract.NAMESPACE == "host-registration@mship"
    assert host_contract.HOSTS_PREFIX == "/hosts"
    assert host_contract.CHALLENGE_PATH == "/hosts/challenge"
    assert host_contract.REGISTER_PATH == "/hosts/register"
    assert host_contract.LIST_PATH == "/hosts"


def test_every_route_path_lives_under_the_single_hosts_prefix():
    assert host_contract.ROUTE_PATHS, "routes must be enumerated here, not inline"
    for path in host_contract.ROUTE_PATHS:
        assert path == host_contract.HOSTS_PREFIX or path.startswith(
            host_contract.HOSTS_PREFIX + "/"
        ), path
    assert host_contract.CHALLENGE_PATH in host_contract.ROUTE_PATHS
    assert host_contract.REGISTER_PATH in host_contract.ROUTE_PATHS
    assert host_contract.LIST_PATH in host_contract.ROUTE_PATHS


def test_fleet_token_header_is_named_once():
    assert host_contract.FLEET_TOKEN_HEADER == "Mship-Fleet-Token"


# --- golden bytes -----------------------------------------------------------


def test_canonical_payload_golden_bytes_over_a_non_ascii_hostname():
    payload = {"label": "wörk-hôst", "host_id": "hst-1", "count": 2}
    assert host_contract.canonical_payload(payload) == (
        b'{"count":2,"host_id":"hst-1","label":"w\xc3\xb6rk-h\xc3\xb4st"}'
    )


def test_canonical_payload_is_insensitive_to_insertion_order():
    a = {"b": 1, "a": {"z": 1, "y": 2}}
    b = {"a": {"y": 2, "z": 1}, "b": 1}
    assert host_contract.canonical_payload(a) == host_contract.canonical_payload(b)


def test_canonical_payload_refuses_values_json_cannot_round_trip():
    # NaN/Infinity are not JSON; emitting them would produce bytes the other
    # end cannot parse, i.e. a signature over garbage.
    with pytest.raises(ValueError):
        host_contract.canonical_payload({"skew": float("nan")})


def test_canonical_payload_is_parseable_json():
    payload = {"label": "wörk-hôst", "host_id": "hst-1"}
    assert json.loads(host_contract.canonical_payload(payload)) == payload


def test_signing_blob_golden_bytes():
    payload = {"label": "wörk-hôst", "host_id": "hst-1", "count": 2}
    assert host_contract.signing_blob("n0nce", payload) == (
        host_contract.NAMESPACE.encode("utf-8")
        + b"\x00n0nce\x00"
        + b'{"count":2,"host_id":"hst-1","label":"w\xc3\xb6rk-h\xc3\xb4st"}'
    )


def test_signing_blob_binds_the_nonce_and_the_payload_together():
    # Neither half may be swapped without changing the bytes (no replay of a
    # signature onto a different payload, or of a payload onto a new nonce).
    p1 = {"host_id": "a"}
    p2 = {"host_id": "b"}
    blobs = {
        host_contract.signing_blob("n1", p1),
        host_contract.signing_blob("n2", p1),
        host_contract.signing_blob("n1", p2),
    }
    assert len(blobs) == 3


# --- identity across the ends -----------------------------------------------


def test_the_enroll_app_reads_the_contract_module_rather_than_copies():
    # The relay end must not restate the namespace, header, or route paths: an
    # equal-but-separate literal drifts silently and 401s / 404s in production
    # while both ends' unit tests stay green.
    from mship.core.relay import enroll_app

    assert enroll_app.host_contract is host_contract


def test_the_enroll_app_declares_no_wire_literals_of_its_own():
    import inspect

    from mship.core.relay import enroll_app

    source = inspect.getsource(enroll_app)
    for literal in (
        host_contract.HOSTS_PREFIX,
        host_contract.FLEET_TOKEN_HEADER,
        host_contract.NAMESPACE,
    ):
        assert f'"{literal}' not in source and f"'{literal}" not in source, literal


def test_the_enroll_app_serves_exactly_the_contract_routes():
    from fastapi.routing import APIRoute

    from mship.core.relay import enroll_app, host_directory
    from mship.core.relay.fleet_token import FleetTokenStore

    import tempfile

    with tempfile.TemporaryDirectory() as base:
        app = enroll_app.build_enroll_app(
            enroll.RequestStore(base),
            relay_domain="relay.example",
            host_directory=host_directory.HostDirectory(
                base, allowed_signers=lambda: "", probe=lambda url: None
            ),
            fleet_tokens=FleetTokenStore(base),
        )
    served = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert set(host_contract.ROUTE_PATHS) <= served
    # …and the pre-existing surface is untouched (it gates cert issuance).
    assert {"/enroll", "/status/{rid}", "/tls-check"} <= served
