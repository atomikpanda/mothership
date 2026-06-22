from mship.core.relay.pairing import parse_pair_link
from mship.core.serve_pair import resolve_advertised_host, serve_pair_link


def test_resolve_concrete_host_passthrough():
    assert resolve_advertised_host("192.168.1.50") == "192.168.1.50"
    assert resolve_advertised_host("host.tailnet.ts.net") == "host.tailnet.ts.net"


def test_resolve_loopback_is_none():
    for h in ("127.0.0.1", "localhost", "::1"):
        assert resolve_advertised_host(h) is None


def test_resolve_unspecified_uses_primary_ip():
    assert resolve_advertised_host("0.0.0.0", primary_ip=lambda: "100.1.2.3") == "100.1.2.3"
    assert resolve_advertised_host("::", primary_ip=lambda: "100.1.2.3") == "100.1.2.3"
    assert resolve_advertised_host("0.0.0.0", primary_ip=lambda: None) is None


def test_serve_pair_link_none_without_token():
    assert serve_pair_link("192.168.1.50", 47100, None, "ws") is None
    assert serve_pair_link("192.168.1.50", 47100, "", "ws") is None


def test_serve_pair_link_none_on_loopback():
    assert serve_pair_link("127.0.0.1", 47100, "secret", "ws") is None


def test_serve_pair_link_concrete_host_round_trips():
    link = serve_pair_link("192.168.1.50", 47100, "secret", "ws")
    assert link is not None and link.startswith("groundcontrol://add?")
    p = parse_pair_link(link)
    assert p["url"] == "http://192.168.1.50:47100"
    assert p["token"] == "secret"
    assert p["workspace"] == "ws"


def test_serve_pair_link_unspecified_uses_detected_ip():
    link = serve_pair_link("0.0.0.0", 47100, "secret", "ws", primary_ip=lambda: "100.1.2.3")
    assert parse_pair_link(link)["url"] == "http://100.1.2.3:47100"
    assert serve_pair_link("0.0.0.0", 47100, "secret", "ws", primary_ip=lambda: None) is None
