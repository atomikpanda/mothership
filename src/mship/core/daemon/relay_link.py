"""The daemon's side of host registration (#471).

One tick-driven object: request an identity-scoped challenge, sign the canonical
payload with the same ed25519 key the tunnel authenticates with, POST it, and
keep doing that forever. Every collaborator is injected (`post`/`clock`/`rng`/
`signer`/`issue_refresh`/`reidentify`) so the whole loop is testable with no
sockets and no sleeps, exactly like `core/relay/health.py`.

Three properties are the reason this is a class and not a function:

- **It never blocks and never prompts.** An unapproved key POSTs `/enroll`
  non-blockingly and re-posts on `ENROLL_REPOST_INTERVAL_S`, rather than
  running `mship relay enroll --wait`'s 1800s polling loop: a VM provisioned at
  02:00 must still be approvable at 09:00, and nobody is at its terminal (AC1,
  AC8). The relay's per-fingerprint dedupe collapses those re-posts to exactly
  one pending record.
- **It backs off without a ceiling.** `TunnelSupervisor`'s
  `backoff_delay * 2 ** restart_count` raises `OverflowError` at 1024 restarts
  — unreachable for a CLI, ~17h for an immortal daemon — so the exponent is
  clamped here, and the delay is jittered from an injected RNG so a fleet
  coming back after a relay redeploy does not retry in lockstep (AC2, AC3).
- **It self-heals from a 409.** A duplicate identity stops the dial; after a
  few consecutive refusals the link re-identifies itself (new `host_id`,
  rotated key) and drops back to awaiting-enrollment, so a cloned VM recovers
  into `GET /hosts` as `pending-approval` without an SSH session (AC4b).

Reconnects write nothing: the refresh credential is *derived*, so re-issuing it
re-publishes the same string and leaves `~/.mothership/daemon/` byte-identical
(AC11).
"""

from __future__ import annotations

import logging
import math
import random
import socket
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from mship import __version__
from mship.core.daemon.capabilities import host_capability_payload
from mship.core.daemon.identity import (
    HostIdentity,
    ensure_host_identity,
    force_reidentify,
    machine_fingerprint,
    mint_instance_id,
)
from mship.core.relay import host_contract, keys, ssh_sig, tunnel
from mship.core.relay.enroll import fingerprint

log = logging.getLogger(__name__)

# The client name the host's own refresh credential is issued under. It is
# published in the directory for whichever phone reads it, so it names the
# channel, not a device.
DIRECTORY_CLIENT = "relay-directory"

# First retry delay; doubles per consecutive failure up to MAX_BACKOFF_S.
RETRY_BASE_S = 2.0

# The de-phasing fraction, re-exported under this module's name: the tunnel
# supervisor jitters its respawn backoff with the same constant and the same
# downward-only formula, so both live in `core/relay/tunnel.py`.
JITTER = tunnel.BACKOFF_JITTER

# DERIVED, not picked: the first exponent at which the delay already exceeds
# the cap. Clamping here is what keeps `2 ** n` from overflowing a float after
# ~17h of failures (assumption 5) — a bound a CLI never reaches and an immortal
# daemon does.
_MAX_EXPONENT = math.ceil(math.log2(host_contract.MAX_BACKOFF_S / RETRY_BASE_S))

# Per-call bound on every relay HTTP call this link makes. Public: the daemon's
# shutdown derives its tunnel-join bound from it (`core/daemon/run.py`), so a
# worst-case tick cannot outlast the wait that exists to prevent an orphan.
HTTP_TIMEOUT_S = 10.0

# States that describe what the RELAY decided about us. A transport blip must
# not erase them: "the relay is unreachable" is not "the relay approved us".
_STICKY_STATES = ("awaiting-enrollment", "duplicate-identity")


def host_subdomain_for(home: Path, host_id: str) -> str:
    """The relay subdomain this host publishes on, derived in ONE place.

    Two callers must agree exactly or an operator approves a key for a name the
    daemon never dials: the link derives it on every identity adoption, and
    `mship daemon reidentify` prints it after minting a new identity.
    """
    pubkey = keys.relay_public_key(keys.ensure_relay_key(home)).strip()
    return tunnel.host_subdomain(
        host_id, tunnel.device_id(pubkey), keys.ensure_subdomain_secret(home)
    )


@dataclass(frozen=True)
class RegistrationOutcome:
    """One registration attempt, as data. Never an exception: the caller is a
    loop that must keep ticking, and `kind` is what the ladder reads."""

    ok: bool
    kind: (
        str  # registered|unapproved|duplicate-identity|refused|transport|signing|error
    )
    detail: str = ""
    refresh: str | None = None
    status_code: int | None = None


class RelayLink:
    """Keeps this host's entry in the relay's directory current."""

    DUPLICATE_REIDENTIFY_AFTER = 3

    def __init__(
        self,
        home: Path,
        relay_cfg,
        *,
        post: Callable | None = None,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        signer: Callable[[bytes], str] | None = None,
        issue_refresh: Callable[[str], str] | None = None,
        reidentify: Callable[[], HostIdentity] | None = None,
        timeout: float = HTTP_TIMEOUT_S,
    ) -> None:
        self._home = Path(home)
        self._relay = relay_cfg
        self._base = host_contract.enroll_base_url(relay_cfg.host)
        self._timeout = timeout
        self._clock = clock  # wall clock: it also dates the skew sample
        self._rng = rng
        self._post = post if post is not None else _default_post
        self._signer = signer if signer is not None else self._sign_with_relay_key
        self._issue_refresh = (
            issue_refresh if issue_refresh is not None else self._issue_from_store
        )
        self._reidentify = (
            reidentify
            if reidentify is not None
            else (lambda: force_reidentify(self._home))
        )

        self.instance_id = mint_instance_id()
        self.state = "unregistered"
        self.last_error: str | None = None
        self.clock_skew_seconds: float | None = None
        self.refresh: str | None = None
        self.failure_count = 0

        self._duplicate_streak = 0
        self._last_attempt_at: float | None = None
        self._last_enroll_at: float | None = None
        self._enroll_repost_interval = host_contract.ENROLL_REPOST_INTERVAL_S
        self._delay = 0.0
        self._adopt(ensure_host_identity(self._home, fingerprint=machine_fingerprint()))

    # -- identity + key material -------------------------------------------

    def _adopt(self, ident: HostIdentity) -> None:
        """Take on an identity and everything derived from it. Called again
        after a re-identify, when both the host_id AND the key have changed."""
        self._identity = ident
        self.host_id = ident.host_id
        key_path = keys.ensure_relay_key(self._home)
        self._pubkey = keys.relay_public_key(key_path).strip()
        self.key_fingerprint = fingerprint(self._pubkey)
        self.subdomain = host_subdomain_for(self._home, ident.host_id)
        self.public_url = f"https://{self.subdomain}.{self._relay.host}"

    def _sign_with_relay_key(self, blob: bytes) -> str:
        return ssh_sig.sign_blob(
            blob,
            key_path=keys.relay_key_path(self._home),
            namespace=host_contract.NAMESPACE,
        )

    def _issue_from_store(self, host_id: str) -> str:
        from mship.core.daemon.host_auth import RefreshStore

        return RefreshStore(self._home, clock=self._clock).issue_refresh(
            host_id=host_id, client=DIRECTORY_CLIENT
        )

    # -- the payload --------------------------------------------------------

    def _payload(self) -> dict:
        """Identity + capability metadata, and nothing else: a registration is
        published to every paired phone, and it is re-sent on every reconnect,
        so anything expensive or private here would be both a leak and a cost."""
        return {
            "host_id": self.host_id,
            "instance_id": self.instance_id,
            "label": socket.gethostname(),
            "machine_fingerprint": self._identity.fingerprint,
            "key_fingerprint": self.key_fingerprint,
            "subdomain": self.subdomain,
            "public_url": self.public_url,
            "mship_version": __version__,
            "refresh": self._issue_refresh(self.host_id),
            **host_capability_payload(),
        }

    # -- one attempt --------------------------------------------------------

    def register_once(self) -> RegistrationOutcome:
        """Challenge → sign → register, as one typed outcome. Never raises for
        anything the relay or the network can do."""
        try:
            challenge = self._post(
                self._base + host_contract.CHALLENGE_PATH,
                json={"key_fingerprint": self.key_fingerprint},
                timeout=self._timeout,
            )
        except Exception as exc:
            return RegistrationOutcome(False, "transport", detail=str(exc))
        self._sample_skew(challenge)
        if challenge.status_code != 200:
            detail = _detail(challenge)
            return RegistrationOutcome(
                False,
                _classify(challenge.status_code, detail),
                detail=detail,
                status_code=challenge.status_code,
            )
        # A 200 is not a promise of JSON: a captive portal, a misrouted vhost or
        # a proxy's error page all answer 200 with HTML, and a `.json()` raising
        # here would break this function's never-raises contract.
        nonce = str((_body(challenge) or {}).get("nonce", ""))
        if not nonce:
            return RegistrationOutcome(
                False,
                "refused",
                status_code=challenge.status_code,
                detail="challenge response carried no nonce "
                f"(is {self._base} really the relay's enroll server?)",
            )

        payload = self._payload()
        try:
            signature = self._signer(host_contract.signing_blob(nonce, payload))
        except Exception as exc:
            return RegistrationOutcome(False, "signing", detail=str(exc))

        try:
            resp = self._post(
                self._base + host_contract.REGISTER_PATH,
                json={"nonce": nonce, "signature": signature, "payload": payload},
                timeout=self._timeout,
            )
        except Exception as exc:
            return RegistrationOutcome(False, "transport", detail=str(exc))
        self._sample_skew(resp)
        if resp.status_code == 200:
            return RegistrationOutcome(
                True, "registered", refresh=payload["refresh"], status_code=200
            )
        detail = _detail(resp)
        return RegistrationOutcome(
            False,
            _classify(resp.status_code, detail),
            detail=detail,
            status_code=resp.status_code,
        )

    def _sample_skew(self, resp) -> None:
        """The enroll server's `Date` is the only header in this loop written by
        a different clock (the read-back's is our own uvicorn's), so it is where
        `mship daemon status` gets `clock_skew_seconds` from. Unparseable or
        absent leaves it unknown rather than guessing zero."""
        raw = (getattr(resp, "headers", None) or {}).get("Date")
        if not raw:
            return
        try:
            self.clock_skew_seconds = (
                self._clock() - parsedate_to_datetime(raw).timestamp()
            )
        except TypeError, ValueError:
            return

    # -- the loop -----------------------------------------------------------

    def tick(self) -> RegistrationOutcome | None:
        """Attempt a registration if one is due, else None. Never raises."""
        now = self._clock()
        if not self._due(now):
            return None
        try:
            outcome = self.register_once()
        except Exception as exc:  # a bug here must not kill the daemon's loop
            log.exception("registration attempt failed unexpectedly")
            outcome = RegistrationOutcome(False, "error", detail=str(exc))
        completed_at = self._clock()
        self._apply(outcome, completed_at)
        self._last_attempt_at = self._clock()
        return outcome

    def _due(self, now: float) -> bool:
        if self._last_attempt_at is None:
            return True
        elapsed = now - self._last_attempt_at
        # A backwards wall-clock step (ntp correction, `timedatectl set-time`)
        # must not park the link until the clock catches up.
        return elapsed < 0 or elapsed >= self._delay

    def _apply(self, outcome: RegistrationOutcome, now: float) -> None:
        if outcome.ok:
            self.state = "registered"
            self.refresh = outcome.refresh
            self.last_error = None
            self.failure_count = 0
            self._duplicate_streak = 0
            self._delay = self._jittered(host_contract.REGISTER_INTERVAL_S)
            return

        self.failure_count += 1
        self.last_error = outcome.detail or outcome.kind
        if outcome.kind == "unapproved":
            self.state = "awaiting-enrollment"
            self._duplicate_streak = 0
            self._post_enroll_if_due(now)
        elif outcome.kind == "duplicate-identity":
            self.state = "duplicate-identity"
            self._duplicate_streak += 1
            if self._duplicate_streak >= self.DUPLICATE_REIDENTIFY_AFTER:
                self._auto_reidentify(now)
        elif self.state not in _STICKY_STATES:
            self.state = "error"
        self._delay = self._jittered(
            min(
                RETRY_BASE_S * 2 ** min(self.failure_count - 1, _MAX_EXPONENT),
                host_contract.MAX_BACKOFF_S,
            )
        )

    def _jittered(self, delay: float) -> float:
        return tunnel.jittered(delay, self._rng)

    def _auto_reidentify(self, now: float) -> None:
        """The relay says someone else holds this identity and we cannot talk it
        out of that. Mint a new one and go back to the enrollment queue — the
        machine that needs this recovery is by definition one nobody can log
        into."""
        log.warning(
            "relay refused %s consecutive registrations as a duplicate identity "
            "(%s); re-identifying automatically — this host will reappear in the "
            "relay's host list as pending-approval and needs approving again",
            self._duplicate_streak,
            self.last_error,
        )
        self._adopt(self._reidentify())
        log.warning("re-identified as %s (subdomain %s)", self.host_id, self.subdomain)
        self._duplicate_streak = 0
        self.state = "awaiting-enrollment"
        self._last_enroll_at = None  # the new key needs approving at once
        self._post_enroll_if_due(now)

    def _post_enroll_if_due(self, now: float) -> None:
        """Keep exactly one live enrollment request in the relay's store.

        Non-blocking and fire-and-forget: no `/status/{rid}` polling, no wait
        loop, no prompt. The relay dedupes by key fingerprint, so re-posting
        refreshes the record's TTL instead of filling the pending cap."""
        if (
            self._last_enroll_at is not None
            and 0 <= now - self._last_enroll_at < self._enroll_repost_interval
        ):
            return
        try:
            response = self._post(
                self._base + host_contract.ENROLL_PATH,
                json={"pubkey": self._pubkey, "hostname": socket.gethostname()},
                timeout=self._timeout,
            )
        except Exception as exc:
            # Leaves `_last_enroll_at` alone, so the next tick retries.
            self.last_error = f"enroll post failed: {exc}"
            return
        if not 200 <= response.status_code < 300:
            self.last_error = (
                _detail(response) or f"enroll post failed: HTTP {response.status_code}"
            )
            return
        body = _body(response)
        if not body or not isinstance(body.get("id"), str) or not body["id"]:
            self.last_error = "enroll post failed: invalid response"
            return
        try:
            ttl = float(body.get("expires_in"))
        except TypeError, ValueError:
            ttl = 0
        if math.isfinite(ttl) and ttl > 0:
            self._enroll_repost_interval = ttl / 3
        self._last_enroll_at = now

    # -- what the tunnel asks ------------------------------------------------

    def should_dial(self) -> bool:
        """False only while the relay says another host holds this identity:
        dialing then would fight a live twin for the subdomain. An unapproved
        key still dials — ssh's refusal is how Task 7 classifies it."""
        return self.state != "duplicate-identity"

    def next_attempt_delay(self) -> float:
        """Seconds from the last attempt until the next one is due."""
        return self._delay

    def register_soon(self) -> None:
        """Make the next `tick()` register instead of waiting out the schedule.

        The tunnel calls this once per respawn (AC2): a reconnected `ssh -R`
        lands on a fresh sish route, so the directory entry must be re-published
        without waiting up to `REGISTER_INTERVAL_S` for it. It only clears the
        schedule — one extra registration per call, never a re-register storm."""
        self._last_attempt_at = None


def _classify(status_code: int, detail: str) -> str:
    """Classify a challenge or registration refusal for the retry loop.

    Both host-registration routes can reject an unapproved key; registration
    can also reject a nonce that expired, raced another attempt, or was already
    spent. Only the former means "wait for a human". Reading the latter as
    unapproved would make an approved host on a slow link re-post `/enroll`.
    The exact details come from `host_contract`; an unrecognised 401 falls back
    to `unapproved` because enrolling once too often is recoverable while never
    enrolling at all is not.
    """
    if status_code == 409:
        return "duplicate-identity"
    if status_code != 401:
        return "refused"
    return (
        "refused" if detail in host_contract.CHALLENGE_REFUSAL_DETAILS else "unapproved"
    )


def _body(resp) -> dict | None:
    """The response's JSON object, or None for anything else — HTML error page,
    truncated body, a bare list. Never raises (the `health.py` discipline)."""
    try:
        body = resp.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _detail(resp) -> str:
    """The relay's own explanation, which is what the operator must read (a 409
    carries the `mship daemon reidentify` hint) and what `_classify` reads to
    tell the two flavors of 401 apart."""
    return str((_body(resp) or {}).get("detail", ""))


def _default_post(url, **kw):
    import httpx

    return httpx.post(url, **kw)
