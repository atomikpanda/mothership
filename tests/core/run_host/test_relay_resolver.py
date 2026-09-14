"""Observable relay binding, expiry, and no-fallback regressions."""
from __future__ import annotations

import json

import httpx
import pytest

from mship.core.run_host.config import (
    RunHostConnection,
    HostRegistration,
    RelayRunHostIdentity,
)
from mship.core.run_host.pairing_store import RelayPairingStore
from mship.core.run_host.resolver import RelayResolutionError, RunHostResolver


def _relay_host(instance: str = "inst-1") -> HostRegistration:
    return HostRegistration(
        "studio", ("ios",), (), 0,
        RelayRunHostIdentity("relay.example", "host-1", "workspace-1", instance),
        "project",
    )


def test_relay_resolution_binds_directory_origin_and_refreshes_only_after_headroom(tmp_path):
    pairing = RelayPairingStore(tmp_path / "private")
    pairing.put("relay.example", "fleet-secret")
    now = [0.0]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "enroll.relay.example":
            assert request.headers["Mship-Fleet-Token"] == "fleet-secret"
            return httpx.Response(200, json={"hosts": [{
                "host_id": "host-1", "state": "online", "instance_id": "inst-1",
                "subdomain": "workspace-abcdef",
                "public_url": "https://workspace-abcdef.relay.example", "refresh": "refresh-secret",
            }]})
        assert request.url == httpx.URL("https://workspace-abcdef.relay.example/host/token")
        assert json.loads(request.content) == {"refresh": "refresh-secret"}
        return httpx.Response(200, json={"token": f"bearer-{len(calls)}", "expires_in": 60})

    resolver = RunHostResolver(
        pairing_store=pairing, transport=httpx.MockTransport(handler), monotonic=lambda: now[0], headroom_s=10,
    )
    first = resolver.resolve(_relay_host())
    now[0] = 20
    cached = resolver.resolve(_relay_host())
    now[0] = 51
    refreshed = resolver.resolve(_relay_host())

    assert first.url == "https://workspace-abcdef.relay.example"
    assert first.identity == ("relay", "relay.example", "host-1", "workspace-1", "inst-1")
    assert cached.token == first.token
    assert refreshed.token != first.token
    assert [request.url.path for request in calls].count("/host/token") == 2
    assert "fleet-secret" not in repr(resolver)
    assert "refresh-secret" not in repr(resolver)


def test_directory_instance_mismatch_never_discloses_refresh_to_host(tmp_path):
    pairing = RelayPairingStore(tmp_path / "private")
    pairing.put("relay.example", "fleet-secret")
    requested_host = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested_host
        if request.url.host != "enroll.relay.example":
            requested_host = True
        return httpx.Response(200, json={"hosts": [{
            "host_id": "host-1", "state": "online", "instance_id": "replacement",
            "subdomain": "workspace-abcdef",
            "public_url": "https://workspace-abcdef.relay.example", "refresh": "refresh-secret",
        }]})

    resolver = RunHostResolver(pairing_store=pairing, transport=httpx.MockTransport(handler))
    with pytest.raises(RelayResolutionError, match="binding changed"):
        resolver.resolve(_relay_host())
    assert not requested_host



def test_directory_public_origin_must_equal_selected_shipped_subdomain(tmp_path):
    pairing = RelayPairingStore(tmp_path / "private")
    pairing.put("relay.example", "fleet-secret")
    requested_host = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested_host
        if request.url.host != "enroll.relay.example":
            requested_host = True
        return httpx.Response(200, json={"hosts": [{
            "host_id": "host-1", "state": "online", "instance_id": "inst-1",
            "subdomain": "workspace-abcdef",
            "public_url": "https://different-abcdef.relay.example", "refresh": "refresh-secret",
        }]})

    resolver = RunHostResolver(pairing_store=pairing, transport=httpx.MockTransport(handler))
    with pytest.raises(RelayResolutionError, match="binding is invalid"):
        resolver.resolve(_relay_host())
    assert not requested_host



def test_directory_rejects_duplicate_json_keys_before_selection(tmp_path):
    pairing = RelayPairingStore(tmp_path / "private")
    pairing.put("relay.example", "fleet-secret")
    resolver = RunHostResolver(
        pairing_store=pairing,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'{"hosts":[],"hosts":[]}')
        ),
    )

    with pytest.raises(RelayResolutionError, match="response is invalid"):
        resolver.resolve(_relay_host())

def test_direct_resolution_never_queries_relay_directory():
    direct = HostRegistration("direct", ("ios",), (), 0, RunHostConnection("http://direct", "token"), "project")
    resolver = RunHostResolver(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected network")))
    resolved = resolver.resolve(direct, force_refresh=True)
    assert (resolved.url, resolved.token, resolved.identity, resolved.expires_at) == (
        "http://direct", "token", ("direct", "http://direct"), None
    )
