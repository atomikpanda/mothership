import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from mship.core.relay.keys import (
    ensure_relay_key,
    ensure_subdomain_secret,
    relay_key_path,
    relay_public_key,
    subdomain_secret_path,
)


def test_ensure_subdomain_secret_creates_stable_0600_secret(tmp_path):
    s1 = ensure_subdomain_secret(home=tmp_path)
    assert isinstance(s1, bytes) and len(s1) >= 32
    path = tmp_path / ".mothership" / "relay-subdomain-secret"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert ensure_subdomain_secret(home=tmp_path) == s1  # stable across calls


def test_ensure_subdomain_secret_tightens_existing_secret_permissions(tmp_path):
    path = tmp_path / ".mothership" / "relay-subdomain-secret"
    path.parent.mkdir(mode=0o755)
    path.write_bytes(b"x" * 32)
    path.chmod(0o644)

    assert ensure_subdomain_secret(home=tmp_path) == b"x" * 32
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_ensure_subdomain_secret_regenerates_truncated_file(tmp_path):
    # A truncated/corrupt persisted secret self-heals rather than yielding a
    # short HMAC key (which would produce subdomains no device recognises).
    path = tmp_path / ".mothership" / "relay-subdomain-secret"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"short")
    s = ensure_subdomain_secret(home=tmp_path)
    assert len(s) >= 32
    assert path.read_bytes() == s
    assert ensure_subdomain_secret(home=tmp_path) == s  # now stable


def test_concurrent_secret_creation_returns_one_persisted_value(tmp_path, monkeypatch):
    from mship.core.relay import keys

    real_open = keys.os.open
    first_open = threading.Event()

    def pause_first_creator(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        if not first_open.is_set():
            first_open.set()
            time.sleep(0.05)
        return fd

    monkeypatch.setattr(keys.os, "open", pause_first_creator)
    barrier = threading.Barrier(8)

    def create():
        barrier.wait()
        return ensure_subdomain_secret(home=tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        secrets = list(executor.map(lambda _: create(), range(8)))

    assert len(set(secrets)) == 1
    assert subdomain_secret_path(tmp_path).read_bytes() == secrets[0]


def test_generates_key_when_absent(tmp_path):
    calls = []

    def fake_run(argv):  # stand in for subprocess
        calls.append(argv)
        key = tmp_path / ".mothership" / "relay_ed25519"
        key.write_text("PRIV")
        (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
        return 0

    path = ensure_relay_key(home=tmp_path, runner=fake_run)
    assert (
        path == tmp_path / ".mothership" / "relay_ed25519"
        or path.name == "relay_ed25519"
    )
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


# --- read-only path accessors (a reporter must not generate keys) ---


def test_paths_are_readable_without_creating_anything(tmp_path):
    assert (
        subdomain_secret_path(tmp_path)
        == tmp_path / ".mothership" / "relay-subdomain-secret"
    )
    assert relay_key_path(tmp_path) == tmp_path / ".mothership" / "relay_ed25519"
    # Read-only: asking for the paths must not create the dir or the files.
    assert not (tmp_path / ".mothership").exists()


def test_ensure_secret_writes_at_the_declared_path(tmp_path):
    secret = ensure_subdomain_secret(home=tmp_path)
    assert subdomain_secret_path(tmp_path).read_bytes() == secret
