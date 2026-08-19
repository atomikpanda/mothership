from __future__ import annotations
import os
import subprocess
import time
from pathlib import Path

from mship.core.relay.enroll import _locked

_SUBDOMAIN_SECRET_LEN = 32


def _default_runner(argv: list[str]) -> int:
    return subprocess.run(argv, check=True).returncode


def _read_secret_if_valid(path: Path, length: int) -> bytes | None:
    """Return the stored secret iff it's present and at least the expected
    length; None if absent, truncated, or still mid-write (a concurrent creator
    that O_EXCL-created the file but hasn't written its bytes yet)."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return data if len(data) >= length else None


def subdomain_secret_path(home: Path) -> Path:
    """Where the per-machine relay-subdomain HMAC secret lives. Read-only —
    creates nothing, so a reporter (see `mship.core.topology`) can check for it
    without generating one."""
    return home / ".mothership" / "relay-subdomain-secret"


def relay_key_path(home: Path) -> Path:
    """Where this machine's relay ssh key lives (public key at `<path>.pub`).
    Read-only — creates nothing."""
    return home / ".mothership" / "relay_ed25519"


def ensure_secret_file(path: Path, length: int = _SUBDOMAIN_SECRET_LEN) -> bytes:
    """Return the random secret stored at `path`, generating it if absent.

    `length` random bytes with mode 0600, created O_EXCL so there is no
    world-readable window. The parent dir is forced to 0700 — mkdir's mode is
    masked by the umask and `exist_ok=True` never tightens an existing loose
    dir, so the corrective chmod is what actually makes it owner-only (the
    `registry.py`/`host_app.py` house pattern).

    Concurrency-safe across threads and processes: the adjacent flock covers
    read/repair/create/write, so every caller returns the same persisted bytes.
    A truncated/corrupt persisted file self-heals by regenerating.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    with _locked(path.with_name(f".{path.name}.lock")):
        existing = _read_secret_if_valid(path, length)
        if existing is not None:
            path.chmod(0o600)
            return existing
        if path.exists():
            # Present but too short → corrupt/truncated; discard and regenerate.
            path.unlink(missing_ok=True)

        secret = os.urandom(length)
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # A non-cooperating/older process may not take our lock. Adopt its
            # completed file rather than returning a divergent secret.
            for _ in range(100):
                adopted = _read_secret_if_valid(path, length)
                if adopted is not None:
                    path.chmod(0o600)
                    return adopted
                time.sleep(0.01)
            raise RuntimeError(f"secret at {path} exists but is unreadable/too short")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secret)
            path.chmod(0o600)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return secret


def ensure_subdomain_secret(home: Path) -> bytes:
    """Return the per-machine relay-subdomain HMAC secret, generating it if absent.

    This secret keys `opaque_slug`, so it must be identical for the two
    subdomain callers (`serve --relay` and `pair`) on the same machine. Losing
    it re-randomizes this machine's subdomains — a one-time re-pair, which is
    acceptable.
    """
    return ensure_secret_file(subdomain_secret_path(home))


def ensure_relay_key(home: Path, runner=_default_runner) -> Path:
    """Return home/.mothership/relay_ed25519, generating it via ssh-keygen if absent."""
    path = relay_key_path(home)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    runner(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-f",
            str(path),
            "-N",
            "",
            "-C",
            "mship-relay",
        ]
    )
    return path


def relay_public_key(path: Path) -> str:
    """Read and return the contents of the public key file at <path>.pub."""
    pub_path = Path(str(path) + ".pub")
    return pub_path.read_text()
