"""Per-workspace Fernet key for encrypted-mode specs (spec-storage-visibility-policy).

The key lives at `<workspace_root>/.mothership/spec-key` (git-ignored — `.mothership/`
is already ignored in an mship workspace; we ensure it defensively). It is a single
symmetric key: the operator holds it and injects it into agents/workers the same way
the run token is injected. Losing it loses every encrypted spec — there is no escrow.
"""
from __future__ import annotations

import sys
from pathlib import Path

from cryptography.fernet import Fernet

from mship.util.git import GitRunner

KEYFILE_RELPATH = Path(".mothership") / "spec-key"

_GENERATED_NOTICE = (
    "\n"
    "  mship generated a new spec encryption key at:\n"
    "    {path}\n"
    "  BACK THIS FILE UP. It is the ONLY key to your encrypted specs.\n"
    "  Losing it makes every encrypted spec permanently unrecoverable — there is\n"
    "  no escrow or recovery. It is git-ignored and never committed or pushed.\n"
)


class SpecKeyMissing(Exception):
    """Encrypted-mode operation needs the key but `.mothership/spec-key` is absent."""


def keyfile_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / KEYFILE_RELPATH


def load_key(workspace_root: Path) -> bytes | None:
    """The raw Fernet key bytes, or None when no keyfile exists (no generation)."""
    path = keyfile_path(workspace_root)
    if not path.is_file():
        return None
    return path.read_bytes()


def require_key(workspace_root: Path) -> bytes:
    """The key, or raise SpecKeyMissing (fail loud — never fall back to plaintext)."""
    key = load_key(workspace_root)
    if key is None:
        raise SpecKeyMissing(
            f"encrypted spec_storage requires a key at {keyfile_path(workspace_root)}, "
            f"but none was found. Generate one with an encrypted write, or restore your backup."
        )
    return key


def load_or_generate_key(workspace_root: Path, *, git: GitRunner | None = None) -> bytes:
    """Return the existing key, else generate + persist one (0600), ensure it is
    git-ignored, and print a loud one-time backup notice to stderr."""
    existing = load_key(workspace_root)
    if existing is not None:
        return existing

    path = keyfile_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Write then tighten perms so the key is never briefly world-readable.
    path.write_bytes(key)
    path.chmod(0o600)

    _ensure_gitignored(Path(workspace_root), git or GitRunner())
    print(_GENERATED_NOTICE.format(path=path), file=sys.stderr)
    return key


def _ensure_gitignored(workspace_root: Path, git: GitRunner) -> None:
    pattern = str(KEYFILE_RELPATH)
    # `.mothership/` is already ignored in a real workspace, so is_ignored is True
    # and we skip. In a bare tmp dir (tests, fresh repo) it is not — append it.
    if not git.is_ignored(workspace_root, pattern):
        git.add_to_gitignore(workspace_root, pattern)


def encrypt(key: bytes, text: str) -> bytes:
    return Fernet(key).encrypt(text.encode("utf-8"))


def decrypt(key: bytes, blob: bytes) -> str:
    return Fernet(key).decrypt(blob).decode("utf-8")
