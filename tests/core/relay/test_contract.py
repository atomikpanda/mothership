"""The relay contract is a single source: the egress-proxy parses by the same
prefixes/header the worker-side config emits, so they can't drift."""
from __future__ import annotations

from mship.core.relay import contract
from mship.core.relay.egress import proxy, request


def test_contract_constants_are_the_relay_shape():
    assert contract.PREFIX_HOST == {"/gh/": "github.com", "/api/": "api.github.com"}
    assert contract.RUN_TOKEN_HEADER == "Mship-Run-Token"
    assert contract.API_PREFIX == "/api/"
    assert contract.GH_PREFIX == "/gh/"


def test_egress_request_parser_uses_the_shared_prefix_map():
    # Same object, not a copy — a change to contract.PREFIX_HOST changes the
    # egress parser too (drift-proof).
    assert request.PREFIX_HOST is contract.PREFIX_HOST


def test_egress_proxy_uses_the_shared_run_token_header():
    assert proxy.RUN_TOKEN_HEADER is contract.RUN_TOKEN_HEADER
