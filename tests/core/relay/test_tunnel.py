from mship.core.relay.tunnel import device_id, device_subdomain, subdomain_for

_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAA mship-relay\n"


def test_device_id_is_stable_short_hex():
    a = device_id(_PUBKEY)
    assert a == device_id(_PUBKEY)              # stable
    assert len(a) == 6 and all(c in "0123456789abcdef" for c in a)


def test_device_id_ignores_comment_and_whitespace():
    # same key body, different trailing comment/whitespace → same id
    assert device_id(_PUBKEY) == device_id("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAA other-comment")


def test_device_id_differs_per_key():
    assert device_id(_PUBKEY) != device_id("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDifferentBodyZZZZ x")


def test_device_subdomain_appends_id_and_is_dns_safe():
    sd = device_subdomain("mship-workspace", "abc123")
    assert sd == "mship-workspace-abc123"
    assert len(sd) <= 63 and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in sd)


def test_device_subdomain_truncates_to_dns_limit():
    sd = device_subdomain("w" * 80, "abc123")
    assert len(sd) <= 63
    assert sd.endswith("-abc123")
    assert not sd.startswith("-") and "--" not in sd
