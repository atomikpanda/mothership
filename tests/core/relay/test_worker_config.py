"""The worker-side git config emitted for a relay boot, pinned to the exact
contract shape (so any drift from the egress-server breaks this test)."""
from __future__ import annotations

from mship.core.relay.worker_config import (
    relay_flags_error,
    relay_git_config_commands,
)


def test_emits_exactly_three_global_config_commands():
    cmds = relay_git_config_commands("https://relay.example", "rt-123")
    assert len(cmds) == 3
    assert all(c.startswith("git config --global ") for c in cmds)


def test_insteadof_rewrites_gh_and_api_prefixes_to_relay():
    cmds = relay_git_config_commands("https://relay.example", "rt-123")
    assert any(
        "url.https://relay.example/gh/.insteadOf" in c and "https://github.com/" in c
        for c in cmds
    )
    assert any(
        "url.https://relay.example/api/.insteadOf" in c and "https://api.github.com/" in c
        for c in cmds
    )


def test_extraheader_carries_the_run_token_under_the_contract_header():
    cmds = relay_git_config_commands("https://relay.example", "rt-123")
    assert any(
        "http.https://relay.example/.extraHeader" in c and "Mship-Run-Token: rt-123" in c
        for c in cmds
    )


def test_trailing_slash_on_relay_url_is_normalized():
    cmds = relay_git_config_commands("https://relay.example/", "rt-1")
    # No doubled slash before the /gh/ prefix.
    assert any("url.https://relay.example/gh/.insteadOf" in c for c in cmds)


def test_relay_flags_error_requires_both_or_neither():
    assert relay_flags_error(None, None) is None
    assert relay_flags_error("https://relay.example", "rt-1") is None
    assert "run-token" in relay_flags_error("https://relay.example", None).lower()
    assert "relay-url" in relay_flags_error(None, "rt-1").lower()
