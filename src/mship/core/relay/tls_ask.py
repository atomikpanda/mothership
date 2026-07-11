from __future__ import annotations
import re

# A serve per-device subdomain LABEL: <base>-<6 hex>, where base is the
# DNS-safe workspace slug ([a-z0-9-]). Mirrors device_subdomain() in tunnel.py.
_SERVE_LABEL = re.compile(r"[a-z0-9][a-z0-9-]*-[0-9a-f]{6}")


def tls_ask_allowed(domain: str, relay_domain: str) -> bool:
    """Whether Caddy may provision an on-demand TLS cert for `domain`.

    True only for the enroll host, the gh-token broker host, and serve
    per-device subdomains under `relay_domain`; False for the bare apex,
    foreign domains, extra subdomain levels, lookalikes, and blank input.
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
    if label in ("enroll", "gh"):
        return True
    return len(label) <= 63 and _SERVE_LABEL.fullmatch(label) is not None
