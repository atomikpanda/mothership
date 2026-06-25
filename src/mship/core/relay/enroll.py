from __future__ import annotations
import base64
import hashlib
import re


def _b64decode_strict(body: str) -> bytes:
    """Decode base64, adding padding if needed, rejecting non-base64 characters."""
    padding = (4 - len(body) % 4) % 4
    padded = body + "=" * padding
    return base64.b64decode(padded, validate=True)


def validate_pubkey(s: str) -> bool:
    """True if `s` is a single ssh public-key line (key-type + decodable base64 body).

    Checks *shape* only — a recognized key-type prefix plus a base64-decodable body
    on a single line. It does not parse the SSH wire format or verify cryptographic
    key structure; the real security boundary for enrollment is owner approval, not
    this function. The single-line guard rejects multi-line / CRLF input so a crafted
    second line cannot be smuggled into the authorized_keys allowlist on approval.
    """
    s = s.strip()
    if "\n" in s or "\r" in s:  # reject multi-line / CRLF authorized_keys injection
        return False
    parts = s.split()
    if len(parts) < 2:
        return False
    ktype, body = parts[0], parts[1]
    if not ktype.startswith(("ssh-", "ecdsa-", "sk-")):
        return False
    try:
        _b64decode_strict(body)
    except Exception:
        return False
    return len(body) >= 20


def fingerprint(pubkey: str) -> str:
    """ssh-keygen-style SHA256 fingerprint of the key body: `SHA256:<base64-no-pad>`."""
    parts = pubkey.strip().split()
    if len(parts) < 2:
        raise ValueError("not an ssh public key")
    body = parts[1]
    digest = hashlib.sha256(_b64decode_strict(body)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def sanitize_label(hostname: str) -> str:
    """A safe pubkeys-filename stem from a hostname: lowercase [a-z0-9-], no traversal, ≤40."""
    s = re.sub(r"[^a-z0-9]+", "-", (hostname or "").lower()).strip("-")[:40].strip("-")
    return s or "device"
