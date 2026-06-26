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


# Phrase emitted by verify_relay_reachable on a 401/403. A stale token never
# recovers by waiting, so wait_until_reachable treats this as terminal.
_AUTH_FAILURE_MARKER = "auth failed"


def wait_until_reachable(public_url: str, token: str, *, get: Callable | None = None,
                         timeout: float = 30.0, interval: float = 3.0,
                         clock: Callable | None = None,
                         sleep: Callable | None = None) -> tuple[bool, str]:
    """Poll `verify_relay_reachable` until reachable or `timeout` seconds elapse.

    A single post-startup probe gives false negatives: after `mship serve --relay`
    starts, the sish route registration, on-demand TLS cert provisioning, and DNS
    propagation each take a few seconds, so the first probe often sees a transport
    error / 404 / 5xx that clears on its own. We retry those transient failures
    every `interval` seconds until the deadline, then return the last detail.

    A 401/403 (stale token) is terminal — retrying can't fix the wrong token — so
    we return immediately rather than burning the whole window.

    `clock`/`sleep` are injectable (default time.monotonic / time.sleep) so tests
    can drive the deadline without real waiting.
    """
    import time as _time
    clock = clock or _time.monotonic
    sleep = sleep or _time.sleep
    deadline = clock() + timeout
    ok, detail = verify_relay_reachable(public_url, token, get=get)
    while not ok and _AUTH_FAILURE_MARKER not in detail and clock() < deadline:
        sleep(interval)
        ok, detail = verify_relay_reachable(public_url, token, get=get)
    return ok, detail
