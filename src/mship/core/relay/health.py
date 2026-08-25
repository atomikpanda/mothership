from __future__ import annotations
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class HealthProbe:
    """Outcome of one `/health` request. Exactly one of `status_code` (a
    response arrived) or `error` (transport/DNS/TLS failure) is set.

    `body`/`date_header` exist for the daemon's tunnel read-back (#471): it asks
    *which* host is answering on its own subdomain, which is a fact only the
    body carries. They are additive — every other caller reads `ok`,
    `status_code` and `error` only.
    """
    ok: bool
    status_code: int | None = None
    error: str | None = None
    body: dict | None = None
    date_header: str | None = None


def probe_health(public_url: str, token: str, *, get: Callable | None = None,
                 timeout: float = 8.0) -> HealthProbe:
    """Probe `<public_url>/health`, authenticating only when a token is provided.

    The single reachability prober for the codebase: `verify_relay_reachable`
    renders its prose from this, and `mship.core.topology` maps the status code
    to a per-edge status + fix hint (a run-host 401 and a phone-pairing 401 need
    different advice, so that mapping belongs with the caller, not here).
    Redirects are reported, never followed: the relay-owned URL must not become
    an attacker-selected request from inside the host.
    """
    if get is None:
        import httpx
        get = lambda url, **kw: httpx.get(url, **kw)
    url = public_url.rstrip("/") + "/health"
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        r = get(url, headers=headers,
                timeout=timeout, follow_redirects=False)
    except Exception as e:  # transport/DNS/TLS error
        return HealthProbe(ok=False, error=str(e))
    code = r.status_code
    return HealthProbe(ok=200 <= code < 300, status_code=code,
                       body=_json_object(r), date_header=_date_header(r))


def _json_object(resp) -> dict | None:
    """The response's JSON object, or None for anything else — an HTML error
    page, a truncated body, a bare list. Never raises: a probe that blew up on a
    captive portal's 200 would turn a reachability check into a crash."""
    try:
        body = resp.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _date_header(resp) -> str | None:
    return (getattr(resp, "headers", None) or {}).get("Date")


def verify_relay_reachable(public_url: str, token: str, *, get: Callable | None = None,
                           timeout: float = 8.0) -> tuple[bool, str]:
    """Probe `<public_url>/health` with the bearer token through the relay.

    Returns (ok, detail). ok=True only on 2xx. 401/403 → a token-mismatch hint.
    Any transport error → ok=False with the exception text (the real reason).
    `get` is injectable (defaults to httpx.get) for testing.
    """
    p = probe_health(public_url, token, get=get, timeout=timeout)
    if p.error is not None:
        return False, f"could not reach relay URL: {p.error}"
    if p.ok:
        return True, "ok"
    if p.status_code in (401, 403):
        return False, (f"relay reachable but auth failed (HTTP {p.status_code}) — "
                       "the paired phone's token is stale; re-scan the QR")
    return False, f"relay returned HTTP {p.status_code}"


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
