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
