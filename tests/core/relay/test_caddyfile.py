"""The relay edge config is part of the contract (AC13).

Every `/hosts` route passes `TestClient` regardless of what Caddy does, so the
one thing unit tests CAN pin about production is the Caddyfile text itself: the
enroll site is hardened to `POST /enroll` + `GET /status/*` with a
`respond "not found" 404` catch-all (`docs/relay-hosting.md`), so without a
matcher placed *before* that catch-all every new route 404s in production while
the whole suite is green.

The second pin is AC9's discriminator: the host classifies a request as
relay-borne from the `X-Forwarded-*` headers the edge stamps. `reverse_proxy`
stamps them by default, so the failure mode is not a missing directive but an
added one that strips them — a config edit that would silently let a
relay-borne request present as loopback and be accepted on the standing token.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from mship.core.daemon.host_app import _EDGE_HEADERS
from mship.core.relay import host_contract

CADDYFILE = Path(__file__).resolve().parents[3] / "docker" / "relay" / "Caddyfile"

# The two sites that front a mship *host*: the enroll site (which now also
# carries the host directory) and the wildcard site that fronts every tunnel.
_ENROLL_SITE = "enroll.{$RELAY_DOMAIN}"
_WILDCARD_SITE = "*.{$RELAY_DOMAIN}"


def _text() -> str:
    return CADDYFILE.read_text()


def _site(name: str) -> str:
    """The body of one top-level site block, by brace matching."""
    text = _text()
    start = text.index(name + " {") + len(name) + 2
    depth = 1
    for i in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i]
    raise AssertionError(f"unbalanced braces around {name!r}")


def test_the_enroll_site_matches_the_hosts_prefix():
    body = _site(_ENROLL_SITE)
    assert re.search(
        r"@hosts\s*\{[^}]*path\s+/hosts\s+/hosts/\*", body
    ), "the @hosts matcher must cover both the bare prefix and everything under it"


def test_every_contract_route_is_covered_by_the_matcher():
    # The matcher is only sufficient while every route stays under one prefix.
    for path in host_contract.ROUTE_PATHS:
        assert path == "/hosts" or path.startswith("/hosts/"), path


def test_the_hosts_handler_precedes_the_404_catch_all():
    body = _site(_ENROLL_SITE)
    handler = body.index("handle @hosts")
    catch_all = body.index('respond "not found" 404')
    assert handler < catch_all, "a handler after the catch-all never runs"


def test_the_hosts_handler_caps_the_request_body():
    body = _site(_ENROLL_SITE)
    handler = body[body.index("handle @hosts"):]
    assert re.search(r"request_body\s*\{[^}]*max_size\s+8KB", handler)


def test_the_hosts_handler_proxies_to_the_enroll_server():
    body = _site(_ENROLL_SITE)
    handler = body[body.index("handle @hosts"):]
    assert "reverse_proxy 127.0.0.1:47180" in handler


@pytest.mark.parametrize("site", [_ENROLL_SITE, _WILDCARD_SITE])
def test_host_facing_sites_reverse_proxy_so_the_edge_stamps_x_forwarded(site):
    # `reverse_proxy` is what sets X-Forwarded-For/-Host/-Proto; a site that
    # `respond`s or rewrites instead would never stamp them.
    assert "reverse_proxy" in _site(site)


@pytest.mark.parametrize("site", [_ENROLL_SITE, _WILDCARD_SITE])
def test_no_directive_strips_the_x_forwarded_headers(site):
    # AC9 reads these headers to refuse the standing token on relay-borne
    # traffic. Deleting or blanking one at the edge fails open, silently.
    body = _site(site).lower()
    for header in _EDGE_HEADERS:
        assert f"header_up -{header}" not in body
        assert f'header_up {header} ""' not in body
        assert f"header_down -{header}" not in body
