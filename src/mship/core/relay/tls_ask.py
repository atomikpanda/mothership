from __future__ import annotations

# A serve per-device subdomain LABEL: <base>-<6 hex>, where base is now an
# opaque per-workspace slug (base32, [a-z2-7]) — the workspace name is no longer
# present. Mirrors device_subdomain() in tunnel.py; base32 ⊂ [a-z0-9].
_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_HEX_CHARS = frozenset("0123456789abcdef")


def host_subdomain_allowed(label: str) -> bool:
    """Whether a label is a tunnel-served host route, excluding relay services."""
    label = (label or "").strip().lower()
    if not 8 <= len(label) <= 63:
        return False
    base, separator, suffix = label.rpartition("-")
    return bool(
        separator
        and base
        and base[0] != "-"
        and all(char in _LABEL_CHARS for char in base)
        and len(suffix) == 6
        and all(char in _HEX_CHARS for char in suffix)
    )


def tls_ask_allowed(domain: str, relay_domain: str) -> bool:
    """Whether Caddy may provision an on-demand TLS cert for `domain`.

    True only for the enroll host and serve per-device subdomains under
    `relay_domain`; False for the bare apex, foreign domains, extra
    subdomain levels, lookalikes, and blank input. (The gh-token broker is
    folded into `mship serve` — it rides a serve subdomain, no separate cert.)
    This is the cert allowlist — keep it tight; a loose match reopens the
    "mint a cert for any host" surface.
    """
    domain = (domain or "").strip().lower()
    relay_domain = (relay_domain or "").strip().lower()
    if not domain or not relay_domain:
        return False
    suffix = "." + relay_domain
    if not domain.endswith(suffix):
        return False
    label = domain[: -len(suffix)]
    if not label or "." in label:        # no nested subdomain levels
        return False
    if label in ("enroll", "egress"):
        return True
    return host_subdomain_allowed(label)
