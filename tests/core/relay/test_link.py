from pathlib import Path

from mship.core.relay.keys import ensure_subdomain_secret, relay_public_key
from mship.core.relay.link import RelayPairLink, build_relay_pair_link
from mship.core.relay.pairing import build_pair_link, parse_pair_link
from mship.core.relay.token import ensure_serve_token
from mship.core.relay.tunnel import device_id, device_subdomain


def _fake_home_with_key(tmp_path):
    home = tmp_path / "home"
    (home / ".mothership").mkdir(parents=True)
    key = home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    return home


def _ws_root_with_token(tmp_path, token="tok-123"):
    ws_root = tmp_path / "ws"
    (ws_root / ".mothership").mkdir(parents=True)
    (ws_root / ".mothership" / "serve-token").write_text(token + "\n")
    return ws_root


def test_build_relay_pair_link_matches_manual_derivation(tmp_path):
    home = _fake_home_with_key(tmp_path)
    ws_root = _ws_root_with_token(tmp_path)

    result = build_relay_pair_link(
        workspace="My WS", host="relay.example.com", workspace_root=ws_root, home=home
    )

    key_path = home / ".mothership" / "relay_ed25519"
    secret = ensure_subdomain_secret(home=home)
    expected_sub = device_subdomain("My WS", device_id(relay_public_key(key_path)), secret)

    assert isinstance(result, RelayPairLink)
    assert result.host == "relay.example.com"
    assert result.subdomain == expected_sub
    assert result.url == f"https://{expected_sub}.relay.example.com"
    assert result.token == "tok-123"
    assert result.link == build_pair_link(url=result.url, token="tok-123", workspace="My WS")
    p = parse_pair_link(result.link)
    assert p == {"url": result.url, "token": "tok-123", "workspace": "My WS"}


def test_token_equals_ensure_serve_token(tmp_path):
    home = _fake_home_with_key(tmp_path)
    ws_root = tmp_path / "ws"
    (ws_root / ".mothership").mkdir(parents=True)  # no seeded token → generated + persisted
    result = build_relay_pair_link(workspace="w", host="h", workspace_root=ws_root, home=home)
    # ac4: same token source as serve, and it persists (re-derives identically).
    assert result.token == ensure_serve_token(ws_root)


def test_two_calls_produce_identical_link_and_token(tmp_path):
    # Simulates the serve path then the pair path with identical inputs (ac3/ac4).
    home = _fake_home_with_key(tmp_path)
    ws_root = _ws_root_with_token(tmp_path)
    serve_side = build_relay_pair_link(
        workspace="w", host="h.example", workspace_root=ws_root, home=home
    )
    pair_side = build_relay_pair_link(
        workspace="w", host="h.example", workspace_root=ws_root, home=home
    )
    assert serve_side.link == pair_side.link
    assert serve_side.token == pair_side.token
    assert serve_side.subdomain == pair_side.subdomain
