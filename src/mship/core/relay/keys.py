from __future__ import annotations
import os
import subprocess
import time
from pathlib import Path

_SUBDOMAIN_SECRET_LEN = 32


def _default_runner(argv: list[str]) -> int:
    return subprocess.run(argv, check=True).returncode


def _read_secret_if_valid(path: Path) -> bytes | None:
    """Return the stored secret iff it's present and at least the expected
    length; None if absent, truncated, or still mid-write (a concurrent creator
    that O_EXCL-created the file but hasn't written its bytes yet)."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return data if len(data) >= _SUBDOMAIN_SECRET_LEN else None


def ensure_subdomain_secret(home: Path) -> bytes:
    """Return the per-machine relay-subdomain HMAC secret, generating it if absent.

    Stored at home/.mothership/relay-subdomain-secret as 32 random bytes with
    mode 0600 (created O_EXCL so there's no world-readable window). This secret
    keys `opaque_slug`, so it must be identical for the two subdomain callers
    (`serve --relay` and `pair`) on the same machine. Losing it re-randomizes
    this machine's subdomains — a one-time re-pair, which is acceptable.

    Concurrency-safe: if two callers race to create it, the loser adopts the
    winner's secret (so both derive the same subdomain) rather than crashing.
    A truncated/corrupt persisted file self-heals by regenerating.
    """
    path = home / ".mothership" / "relay-subdomain-secret"
    existing = _read_secret_if_valid(path)
    if existing is not None:
        return existing
    if path.exists():
        # Present but too short → corrupt/truncated; discard and regenerate.
        path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = os.urandom(_SUBDOMAIN_SECRET_LEN)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Lost a concurrent-creation race — adopt the winner's secret so both
        # subdomain callers agree. Briefly retry in case it was O_EXCL-created
        # but the 32 bytes haven't landed yet.
        for _ in range(100):
            adopted = _read_secret_if_valid(path)
            if adopted is not None:
                return adopted
            time.sleep(0.01)
        raise RuntimeError(
            f"relay subdomain secret at {path} exists but is unreadable/too short"
        )
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
