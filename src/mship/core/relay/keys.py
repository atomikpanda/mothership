from __future__ import annotations
import subprocess
from pathlib import Path


def _default_runner(argv: list[str]) -> int:
    return subprocess.run(argv, check=True).returncode


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
