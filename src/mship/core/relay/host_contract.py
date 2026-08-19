"""Single source of truth for the host-registration contract (#471).

Three ends must agree, and none of them can see the others' code:

- the daemon's relay link (`core/daemon/relay_link.py`) SIGNS a payload and
  POSTs it,
- the relay's enroll app (`core/relay/enroll_app.py`) VERIFIES the same bytes
  and serves the directory,
- Ground Control READS the directory over the same routes.

Everything they must agree on lives here — the ssh signature namespace, the
header the phone carries its fleet token in, the route paths, the TTLs, and
above all `canonical_payload`/`signing_blob`. Key order, separators and
escaping are free parameters of `json.dumps`: two ends that pick differently
verify fine in one interpreter and 401 on every registration in production, so
the bytes are pinned here and golden-tested, never re-derived at a call site.
"""

from __future__ import annotations

import json

# `ssh-keygen -Y sign -n <namespace>`: binds a signature to THIS use, so a
# signature made for anything else cannot be replayed as a registration.
NAMESPACE = "host-registration@mship"

# The phone's per-device fleet credential (relay-issued, revocable per label).
FLEET_TOKEN_HEADER = "Mship-Fleet-Token"

# AC13: the enroll site is hardened to POST /enroll + GET /status/* with a 404
# catch-all, so every route below must sit under ONE prefix — that is what
# keeps a single `@hosts { path /hosts /hosts/* }` Caddy matcher sufficient.
HOSTS_PREFIX = "/hosts"

# The enroll site's public name, derived from the relay domain. One spelling:
# `mship relay enroll` (a human on a new device) and the daemon's relay link (a
# headless host) must reach the SAME server, or a host enrolls where nobody is
# looking for it.
ENROLL_SUBDOMAIN = "enroll"

LIST_PATH = HOSTS_PREFIX
CHALLENGE_PATH = f"{HOSTS_PREFIX}/challenge"
REGISTER_PATH = f"{HOSTS_PREFIX}/register"
ROUTE_PATHS: tuple[str, ...] = (LIST_PATH, CHALLENGE_PATH, REGISTER_PATH)

# The pre-existing enrollment route, spelled once: the daemon's relay link, the
# `mship relay enroll` CLI and the app that serves it must all name it the same.
ENROLL_PATH = "/enroll"

# Challenge issuance and registration both use 401. The exact detail tells the
# daemon whether the key is unapproved (enroll and wait for a human) or the
# signed registration lost a race on its 120s nonce (retry). The strings are
# therefore wire-contract values read by both ends, not presentation text.
UNAPPROVED_KEY_DETAIL = "registration is not signed by an approved key"
MALFORMED_NONCE_DETAIL = "malformed nonce"
UNKNOWN_NONCE_DETAIL = "unknown or already-used nonce"
EXPIRED_CHALLENGE_DETAIL = "challenge expired"
CHALLENGE_REFUSAL_DETAILS: tuple[str, ...] = (
    MALFORMED_NONCE_DETAIL,
    UNKNOWN_NONCE_DETAIL,
    EXPIRED_CHALLENGE_DETAIL,
)

# A nonce only has to survive one round trip; short because it is single-use
# and a replay window is the whole point of issuing one.
CHALLENGE_TTL_S = 120

# The bearer the phone carries. Owned here (published to both ends), minted by
# `core/daemon/host_token.py`, which imports it rather than restating it.
HOST_TOKEN_TTL_S = 300

# How often a healthy daemon re-registers, and the cap on its reconnect
# backoff (mirrors `TunnelSupervisor(max_backoff_delay=...)`, pinned by test).
REGISTER_INTERVAL_S = 60.0
MAX_BACKOFF_S = 60.0

# DERIVED, never a hand-picked number: a host that misses beats and reconnects
# on the worst-case backoff must still be inside the window, or healthy hosts
# flap to `offline` on every reconnect (AC10).
DIRECTORY_STALE_S = 3 * REGISTER_INTERVAL_S + MAX_BACKOFF_S

# How long the relay's enroll store keeps an unapproved request (the store's
# default TTL — `enroll.RequestStore` imports this).
ENROLL_TTL_S = 1800

# The daemon re-posts its enroll request on this schedule while awaiting
# approval. Strictly shorter than the store's TTL, so a VM provisioned at 02:00
# is still approvable at 09:00 (AC1/AC8).
ENROLL_REPOST_INTERVAL_S = ENROLL_TTL_S // 3


def enroll_base_url(relay_host: str) -> str:
    """`https://enroll.<relay domain>` — the base every route above hangs off."""
    return f"https://{ENROLL_SUBDOMAIN}.{relay_host.strip().rstrip('.')}"


def canonical_payload(payload: dict) -> bytes:
    """The exact bytes both ends sign and verify.

    `sort_keys` removes insertion order, the compact separators remove
    whitespace, `ensure_ascii=False` + explicit utf-8 keeps a non-ASCII
    hostname one stable encoding rather than two (`\\u00f4` vs the raw bytes),
    and `allow_nan=False` fails loud instead of emitting `NaN`, which is not
    JSON and which the other end could not parse back.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def signing_blob(nonce: str, payload: dict) -> bytes:
    """`namespace ‖ nonce ‖ canonical_payload(payload)`, NUL-separated.

    The nonce is inside the signed bytes, so a captured signature cannot be
    replayed against a fresh challenge, and the payload is inside them, so a
    signature cannot be lifted onto a different payload. NUL is the separator
    because it can appear in neither the namespace nor the nonce.
    """
    return b"\x00".join(
        (NAMESPACE.encode("utf-8"), nonce.encode("utf-8"), canonical_payload(payload))
    )
