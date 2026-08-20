"""The ssh-keygen signature boundary (#471).

The daemon proves its identity with the SAME ed25519 key sish authenticates its
tunnel with, verified against the SAME `pubkeys/` allowlist — so signature-auth
and tunnel-auth are one identity and the relay never has to assert on the
daemon's behalf. `ssh-keygen -Y sign` / `-Y verify` do the crypto; this module
is only the subprocess boundary, with an injected `runner` (the
`keys._default_runner` pattern) so every caller is testable without a shell.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from mship.core.relay.enroll import fingerprint, validate_pubkey


class SignatureError(Exception):
    """Signing failed, or verification could not be carried out at all.

    Deliberately typed: callers map this to a 5xx/"cannot sign" state, which is
    a different thing from `verify_blob` returning False (a bad signature =
    401). A bare `CalledProcessError` escaping here would leak the argv, which
    contains the private key path, into an HTTP error surface.
    """


def _default_runner(argv: list[str], input_bytes: bytes) -> subprocess.CompletedProcess:
    """Run ssh-keygen with the blob on stdin. No `check=True`: a non-zero exit
    is data here (a bad signature), not an exception."""
    return subprocess.run(argv, input=input_bytes, capture_output=True)


def _run(argv: list[str], input_bytes: bytes, runner: Callable) -> subprocess.CompletedProcess:
    try:
        return runner(argv, input_bytes)
    except Exception as e:  # missing ssh-keygen, CalledProcessError, OSError…
        raise SignatureError(f"ssh-keygen could not be run: {e}") from e


def _stderr(proc: subprocess.CompletedProcess) -> str:
    err = proc.stderr or b""
    return err.decode("utf-8", "replace").strip() if isinstance(err, bytes) else str(err).strip()


def sign_blob(
    blob: bytes,
    *,
    key_path: Path,
    namespace: str,
    runner: Callable = _default_runner,
) -> str:
    """Return the armored SSHSIG over `blob`, signed with the private key at
    `key_path`. Raises `SignatureError` if ssh-keygen refuses."""
    argv = [
        "ssh-keygen", "-Y", "sign",
        "-f", str(key_path),
        "-n", namespace,
        "-q",
    ]
    proc = _run(argv, blob, runner)
    if proc.returncode != 0:
        raise SignatureError(f"ssh-keygen sign failed: {_stderr(proc)}")
    out = proc.stdout or b""
    return (out.decode("utf-8") if isinstance(out, bytes) else out).strip()


def verify_blob(
    blob: bytes,
    *,
    signature: str,
    identity: str,
    allowed_signers: str,
    namespace: str,
    runner: Callable = _default_runner,
) -> bool:
    """True iff `signature` is a valid SSHSIG over `blob` for `namespace`, made
    by the key `allowed_signers` lists under principal `identity`.

    False is the ordinary "not signed by an approved key" answer; only an
    inability to *run* the check raises.
    """
    if not signature.strip() or not allowed_signers.strip():
        # No approved key could have signed it — refuse without spawning.
        return False
    with tempfile.TemporaryDirectory(prefix="mship-sshsig.") as td:
        signers = Path(td) / "allowed_signers"
        sig = Path(td) / "signature"
        signers.write_text(allowed_signers if allowed_signers.endswith("\n")
                           else allowed_signers + "\n")
        sig.write_text(signature if signature.endswith("\n") else signature + "\n")
        argv = [
            "ssh-keygen", "-Y", "verify",
            "-f", str(signers),
            "-I", identity,
            "-n", namespace,
            "-s", str(sig),
        ]
        return _run(argv, blob, runner).returncode == 0


def build_allowed_signers(pubkeys_dir) -> str:
    """The `pubkeys/` allowlist rendered as an ssh allowed_signers file.

    One line per approved key, principal = that key's fingerprint (which is
    what a registration payload claims, so the relay verifies against exactly
    the key the payload names). Filenames and subdirectories are ignored,
    matching sish's recursive directory loader. Anything `validate_pubkey`
    rejects is skipped — the same guard that keeps a smuggled second line out
    of both allowlists.
    """
    directory = Path(pubkeys_dir)
    lines: list[str] = []
    for path in sorted(directory.rglob("*")) if directory.is_dir() else []:
        try:
            key = path.read_text().strip()
        except (OSError, UnicodeError):
            continue
        if not validate_pubkey(key):
            continue
        ktype, body = key.split()[:2]
        lines.append(f"{fingerprint(key)} {ktype} {body}")
    return "".join(f"{line}\n" for line in lines)


def revoke_allowed_key(pubkeys_dir, identity: str) -> int:
    """Remove every allowlist file containing `identity`; return the count."""
    directory = Path(pubkeys_dir)
    removed = 0
    for path in sorted(directory.rglob("*")) if directory.is_dir() else []:
        try:
            key = path.read_text().strip()
        except (OSError, UnicodeError):
            continue
        if not validate_pubkey(key) or fingerprint(key) != identity:
            continue
        path.unlink()
        removed += 1
    return removed
