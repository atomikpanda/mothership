"""Private coordinator relay-pairing regressions."""
from __future__ import annotations

import pytest

from mship.core.relay.pairing import parse_relay_account_link
from mship.core.run_host.pairing_store import RelayPairingStore
from mship.core.run_host.store import RunHostError


def test_relay_account_link_round_trips_only_its_existing_wire_shape(tmp_path):
    parsed = parse_relay_account_link("groundcontrol://add-relay?relay=Relay.Example.&token=a%2Bb%3D%3D")
    store = RelayPairingStore(tmp_path / "private")
    store.put(parsed["relay"], parsed["token"])

    assert store.get("relay.example") == "a+b=="
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert "a+b==" not in repr(store)


@pytest.mark.parametrize("link", [
    "groundcontrol://add?url=https%3A%2F%2Fhost&token=x&workspace=w",
    "groundcontrol://add-relay?relay=relay.example&token=x&token=y",
    "groundcontrol://add-relay?relay=relay.example",
])
def test_relay_pairing_rejects_direct_duplicate_or_incomplete_links(link):
    with pytest.raises(ValueError):
        parse_relay_account_link(link)


def test_pairing_store_does_not_treat_missing_pairing_as_a_fallback(tmp_path):
    with pytest.raises(RunHostError, match="pairing is missing"):
        RelayPairingStore(tmp_path / "private").get("relay.example")


def test_mixed_relay_connection_config_fails_closed_without_echoing_secret(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    secret = "must-not-leak"
    (state / "run-hosts.yaml").write_text(
        "version: 2\nhosts:\n  studio:\n    roles: [ios]\n    tags: []\n"
        "    preference: 0\n    connection:\n      mode: relay\n"
        f"      relay: relay.example\n      host_id: host\n      workspace_id: ws\n      instance_id: instance\n      token: {secret}\n"
    )
    from mship.core.run_host.store import RunHostStore

    with pytest.raises(RunHostError) as error:
        RunHostStore(state).effective_hosts()
    assert secret not in str(error.value)


@pytest.mark.parametrize("workspace_id", ["../other", "ws/other", "ws.other", "ws\x00other"])
def test_relay_workspace_identity_rejects_path_substitution(workspace_id):
    from mship.core.run_host.config import RelayRunHostIdentity

    with pytest.raises(ValueError, match="safe route identifiers"):
        RelayRunHostIdentity("relay.example", "host-1", workspace_id, "instance-1")
