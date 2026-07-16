from __future__ import annotations
import os
import subprocess
from pathlib import Path


def _default_runner(argv: list[str]) -> int:
    return subprocess.run(argv, check=True).returncode


def ensure_subdomain_secret(home: Path) -> bytes:
    """Return the per-machine relay-subdomain HMAC secret, generating it if absent.

    Stored at home/.mothership/relay-subdomain-secret as 32 random bytes with
    mode 0600 (created O_EXCL so there's no world-readable window). This secret
    keys `opaque_slug`, so it must be identical for the two subdomain callers
    (`serve --relay` and `pair`) on the same machine. Losing it re-randomizes
    this machine's subdomains — a one-time re-pair, which is acceptable.
    """
    path = home / ".mothership" / "relay-subdomain-secret"
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = os.urandom(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    return secret


def ensure_relay_key(home: Path, runner=_default_runner) -> Path:
    """Return home/.mothership/relay_ed25519, generating it via ssh-keygen if absent."""
    path = home / ".mothership" / "relay_ed25519"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    runner([
        "ssh-keygen",
        "-t", "ed25519",
        "-f", str(path),
        "-N", "",
        "-C", "mship-relay",
    ])
    return path


def relay_public_key(path: Path) -> str:
    """Read and return the contents of the public key file at <path>.pub."""
    pub_path = Path(str(path) + ".pub")
    return pub_path.read_text()
