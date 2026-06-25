from __future__ import annotations
from typing import Callable


def verify_relay_reachable(public_url: str, token: str, *, get: Callable | None = None,
                           timeout: float = 8.0) -> tuple[bool, str]:
    """Probe `<public_url>/health` with the bearer token through the relay.

    Returns (ok, detail). ok=True only on 2xx. 401/403 → a token-mismatch hint.
    Any transport error → ok=False with the exception text (the real reason).
    `get` is injectable (defaults to httpx.get) for testing.
    """
    if get is None:
        import httpx
        get = lambda url, **kw: httpx.get(url, **kw)
    url = public_url.rstrip("/") + "/health"
    try:
        r = get(url, headers={"Authorization": f"Bearer {token}"},
                timeout=timeout, follow_redirects=True)
    except Exception as e:  # transport/DNS/TLS error
        return False, f"could not reach relay URL: {e}"
    if 200 <= r.status_code < 300:
        return True, "ok"
    if r.status_code in (401, 403):
        return False, (f"relay reachable but auth failed (HTTP {r.status_code}) — "
                       "the paired phone's token is stale; re-scan the QR")
    return False, f"relay returned HTTP {r.status_code}"
