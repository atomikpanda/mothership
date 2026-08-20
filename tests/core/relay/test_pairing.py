from mship.core.relay.pairing import build_pair_link, parse_pair_link

def test_round_trip():
    link = build_pair_link(url="https://ws.relay.example.com", token="tok en/+=", workspace="ws")
    assert link.startswith("groundcontrol://add?")
    p = parse_pair_link(link)
    assert p == {"url": "https://ws.relay.example.com", "token": "tok en/+=", "workspace": "ws"}

def test_parse_rejects_wrong_scheme():
    import pytest
    with pytest.raises(ValueError):
        parse_pair_link("https://add?url=x")


def test_relay_account_link_carries_the_relay_and_the_fleet_token():
    from mship.core.relay.pairing import build_relay_account_link

    link = build_relay_account_link(relay="relay.example.com", token="abc.def+/=")
    assert link.startswith("groundcontrol://add-relay?")
    # quote (not quote_plus), so the token round-trips byte-for-byte.
    assert "relay=relay.example.com" in link
    assert "token=abc.def%2B%2F%3D" in link
    assert " " not in link
