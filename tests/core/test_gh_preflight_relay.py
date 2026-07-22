"""Relay-attach mode of run_preflight: one verification (permissions.push),
routed through <relay>/api + the Mship-Run-Token header. No live services."""
from __future__ import annotations

import httpx

from mship.core.gh_preflight import run_preflight


def _run(handler, **overrides):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kwargs = dict(
        explicit_token=None, broker_url=None, broker_bearer=None,
        repos=["lib"], repo_owner_names={"lib": "acme/lib"},
        relay_url="https://relay.example", run_token="rt-1", client=client,
    )
    kwargs.update(overrides)
    return run_preflight(**kwargs)


def test_relay_probes_api_leg_with_run_token_header_and_oks_on_200_push():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        seen["run_token"] = request.headers.get("Mship-Run-Token")
        seen["has_auth"] = "authorization" in {k.lower() for k in request.headers}
        return httpx.Response(200, json={"permissions": {"push": True}})

    result = _run(handler)
    assert result.ok, result.message
    assert "acme/lib" in result.message
    assert seen["path"] == "/api/repos/acme/lib"
    assert seen["url"] == "https://relay.example/api/repos/acme/lib"
    assert seen["run_token"] == "rt-1"
    assert seen["has_auth"] is False        # relay attaches creds; no bearer from the worker


def test_relay_401_is_invalid_or_expired():
    result = _run(lambda r: httpx.Response(401, json={"message": "bad"}))
    assert not result.ok
    assert "invalid or expired" in result.message.lower()


def test_relay_403_cannot_push():
    result = _run(lambda r: httpx.Response(403, json={"message": "denied"}))
    assert not result.ok
    assert "cannot push" in result.message.lower()
    assert "acme/lib" in result.message


def test_relay_200_without_push_cannot_push():
    result = _run(lambda r: httpx.Response(200, json={"permissions": {"push": False}}))
    assert not result.ok
    assert "cannot push" in result.message.lower()


def test_relay_unreachable_is_a_clear_failure():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    result = _run(handler)
    assert not result.ok
    assert "relay.example" in result.message


def test_relay_mode_does_not_fall_through_to_override_token():
    # A stray GH token/env must be ignored when relay flags are present.
    result = _run(
        lambda r: httpx.Response(200, json={"permissions": {"push": True}}),
        explicit_token="should-not-be-used",
    )
    assert result.ok
    assert "relay covers" in result.message.lower()
