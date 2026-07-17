from pathlib import Path
from mship.core.relay.keys import ensure_relay_key, ensure_subdomain_secret, relay_public_key


def test_ensure_subdomain_secret_creates_stable_0600_secret(tmp_path):
    s1 = ensure_subdomain_secret(home=tmp_path)
    assert isinstance(s1, bytes) and len(s1) >= 32
    path = tmp_path / ".mothership" / "relay-subdomain-secret"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert ensure_subdomain_secret(home=tmp_path) == s1   # stable across calls


def test_ensure_subdomain_secret_regenerates_truncated_file(tmp_path):
    # A truncated/corrupt persisted secret self-heals rather than yielding a
    # short HMAC key (which would produce subdomains no device recognises).
    path = tmp_path / ".mothership" / "relay-subdomain-secret"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"short")
    s = ensure_subdomain_secret(home=tmp_path)
    assert len(s) >= 32
    assert path.read_bytes() == s
    assert ensure_subdomain_secret(home=tmp_path) == s   # now stable

def test_generates_key_when_absent(tmp_path):
    calls = []
    def fake_run(argv):                      # stand in for subprocess
        calls.append(argv)
        key = tmp_path / ".mothership" / "relay_ed25519"
        key.write_text("PRIV"); (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
        return 0
    path = ensure_relay_key(home=tmp_path, runner=fake_run)
    assert path == tmp_path / ".mothership" / "relay_ed25519" or path.name == "relay_ed25519"
    assert any("ssh-keygen" in a for a in calls[0])
    assert relay_public_key(path).startswith("ssh-ed25519 ")

def test_idempotent_when_present(tmp_path):
    # pre-create the key; runner must NOT be called
    mothership_dir = tmp_path / ".mothership"
    mothership_dir.mkdir(parents=True, exist_ok=True)
    key_path = mothership_dir / "relay_ed25519"
    key_path.write_text("PRIV")
    pub_path = Path(str(key_path) + ".pub")
    pub_path.write_text("ssh-ed25519 BBBB mship-relay\n")

    calls = []
    def fake_run(argv):
        calls.append(argv)
        return 0

    path = ensure_relay_key(home=tmp_path, runner=fake_run)
    assert len(calls) == 0, "runner must NOT be called when key already exists"
    assert path == key_path
