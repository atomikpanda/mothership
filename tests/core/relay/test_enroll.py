import pytest

from mship.core.relay.enroll import validate_pubkey, fingerprint, sanitize_label

_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAAAAAA host"


def test_validate_accepts_ssh_key():
    assert validate_pubkey(_PUB)
    assert validate_pubkey("ssh-rsa AAAAB3NzaC1yc2EAAAAD host")


def test_validate_rejects_junk():
    assert not validate_pubkey("not a key")
    assert not validate_pubkey("")
    assert not validate_pubkey("ssh-ed25519 !!!notbase64!!!")
    assert not validate_pubkey("rm -rf /")


def test_validate_rejects_multiline_injection():
    # A crafted second line must not be smuggled into the authorized_keys allowlist.
    assert not validate_pubkey(_PUB + "\n" + _PUB.replace("host", "evil"))
    assert not validate_pubkey(_PUB + "\r\n" + _PUB.replace("host", "evil"))


def test_fingerprint_is_stable_sha256():
    fp = fingerprint(_PUB)
    assert fp.startswith("SHA256:")
    assert fp == fingerprint(_PUB + "  different-comment")  # body only


def test_fingerprint_rejects_non_key():
    with pytest.raises(ValueError):
        fingerprint("ssh-ed25519")


def test_sanitize_label_is_traversal_proof():
    assert sanitize_label("../../etc/passwd") == "etc-passwd"
    assert sanitize_label("My Laptop!") == "my-laptop"
    assert sanitize_label("") == "device"
    s = sanitize_label("a/" * 100)
    assert "/" not in s and ".." not in s and len(s) <= 40
