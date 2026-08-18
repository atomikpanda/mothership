"""The relay's host directory (#471): who is out there, and who may claim it.

`<store-dir>/hosts/<host_id>.json` + `<store-dir>/challenges/<nonce>.json`,
atomic tmp-replace writes and a lazy TTL sweep — the `enroll.RequestStore`
shape, for the same reason: the enroll server restarts and the fleet must
survive it.

Two invariants carry the security of this module:

- **The relay never asserts identity.** Every write is gated on a signature it
  verified against the `pubkeys/` allowlist sish itself authenticates against.
  There is exactly one entry-write call site (in `register`, after the
  signature check) and it is the only caller of `_write_atomic` for `hosts/`.
- **Only the relay's clock decides freshness.** `last_seen`, challenge issue
  and expiry, staleness and takeover eligibility are stamped from the injected
  clock, never from a payload field — a VM whose wall clock stepped an hour
  must not be able to render itself offline or become hijackable (AC10).

Restart vs clone is arbitrated by PROBING the incumbent, not by comparing
fingerprints: `cp -a` copies the machine fingerprint verbatim, so a
fingerprint-keyed check would read the clone as an idempotent re-registration
and silently overwrite the incumbent's URL and credential (decision f).
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence

from mship.core.relay import host_contract, ssh_sig

# host_ids are `hst-<timestamp>-<hex>` (`core.daemon.identity.mint_host_id`).
# Anything else is rejected before it is used as a path component — the
# `enroll._RID_RE` precedent, and the reason a traversal-shaped id never
# reaches the filesystem.
_HOST_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_NONCE_RE = re.compile(r"\A[0-9a-f]{1,64}\Z")

# Copied from the payload onto the entry. Anything else a future daemon sends
# is ignored rather than stored: the directory is a published surface.
_PAYLOAD_FIELDS = (
    "instance_id",
    "label",
    "key_fingerprint",
    "machine_fingerprint",
    "subdomain",
    "public_url",
    "mship_version",
    "capabilities",
    "runner",
    "refresh",
)

# Bounded so a public, unauthenticated route cannot fill the relay's disk. Sized
# far above any real fleet: every host asks for at most one challenge per
# registration, and a challenge lives CHALLENGE_TTL_S.
MAX_CHALLENGES = 1000

_REIDENTIFY_HINT = (
    "another live host already claims this host_id; "
    "run `mship daemon reidentify` on the new machine"
)


def _as_float(value, default: float = 0.0) -> float:
    """A finite timestamp read back from disk, or `default` if it is not one.

    A hand-edited entry must degrade to "very old" (→ offline, takeover-
    eligible) rather than raise and brick every listing. NaN and infinities are
    rejected for the same reason and not merely for tidiness: `now - nan >=
    stale` is False, so a NaN `last_seen` would read as permanently ONLINE and
    un-takeoverable — the exact opposite of the intended degradation.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def probe_instance_id(public_url: str, *, get: Callable | None = None,
                      timeout: float = 5.0) -> str | None:
    """Ask an incumbent's published URL who it is: `GET <url>/health` →
    `instance_id`, or None if it does not answer recognisably.

    The production `probe` for `HostDirectory`, kept a free function (and
    injectable via `get`) because the directory takes its prober as a required
    argument — a directory that cannot probe cannot tell a restart from a clone,
    so it must never quietly default to one. Unauthenticated on purpose:
    `/health` needs no bearer (it exposes only ids and counts), and the relay
    holds no credential for a host it is arbitrating.

    Transport failures propagate; `_arbitrate` already treats an unreachable
    incumbent as "yields the entry" and is the only caller.
    """
    if get is None:
        import httpx

        get = httpx.get
    response = get(public_url.rstrip("/") + "/health", timeout=timeout,
                   follow_redirects=True)
    if response.status_code != 200:
        return None
    body = response.json()
    return body.get("instance_id") if isinstance(body, dict) else None


class ChallengeRefused(Exception):
    """The nonce is unknown, already used, or expired (401)."""


class ChallengeCapReached(Exception):
    """Too many outstanding challenges to mint another (429)."""


class SignatureRefused(Exception):
    """The payload was not signed by the approved key it names (401)."""


class DuplicateIdentity(Exception):
    """A different live instance already holds this host_id (409)."""


class InvalidHostId(ValueError):
    """The host_id is not a well-formed host id (400)."""


class HostDirectory:
    """Filesystem-backed host directory with signature-gated writes."""

    def __init__(
        self,
        base_dir,
        *,
        allowed_signers: Callable[[], str],
        probe: Callable[[str], str | None],
        verify: Callable[..., bool] = ssh_sig.verify_blob,
        clock: Callable[[], float] = time.time,
        challenge_ttl_s: float = host_contract.CHALLENGE_TTL_S,
        stale_after_s: float = host_contract.DIRECTORY_STALE_S,
        max_challenges: int = MAX_CHALLENGES,
    ) -> None:
        """`allowed_signers` is re-read per verification (an approval between
        two registrations must take effect without restarting the server), and
        `probe(public_url) -> instance_id | None` is the arbitration probe —
        required, not defaulted, because a directory that cannot probe cannot
        tell a restart from a clone."""
        base = Path(base_dir)
        self._hosts = base / "hosts"
        self._challenges = base / "challenges"
        for directory in (self._hosts, self._challenges):
            # Owner-only: an entry carries the phone's refresh credential. The
            # corrective chmod is not redundant — mkdir's mode is masked by the
            # umask and `exist_ok=True` never tightens an already-loose dir
            # (the `registry.py`/`host_token.py` house pattern).
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        self._allowed_signers = allowed_signers
        self._probe = probe
        self._verify = verify
        self._clock = clock
        self._challenge_ttl = challenge_ttl_s
        self._stale_after = stale_after_s
        self._max_challenges = max_challenges

    # --- storage primitives -------------------------------------------------

    def _write_atomic(self, path: Path, rec: dict) -> None:
        """Owner-only atomic write (`host_token._save` shape): mkstemp creates
        the file 0600, so an entry's refresh credential is never briefly
        world-readable, and the unique temp name means two writers to the same
        entry cannot scribble over each other's half-written temp file."""
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                         dir=path.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(json.dumps(rec, sort_keys=True))
            os.replace(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def _read_rec(self, path: Path) -> dict | None:
        """Load a record, quarantining a corrupt/truncated file instead of
        raising: one hand-edited file must not brick the whole directory
        (`RequestStore._read_rec` precedent)."""
        try:
            rec = json.loads(path.read_text())
            if not isinstance(rec, dict):
                raise ValueError("not an object")
            return rec
        except (json.JSONDecodeError, OSError, ValueError):
            try:
                path.replace(path.with_suffix(".json.corrupt"))
            except OSError:
                path.unlink(missing_ok=True)
            return None

    # --- challenges ---------------------------------------------------------

    def _read_challenge(self, path: Path) -> dict | None:
        """Load a challenge, DELETING a corrupt one rather than quarantining
        it: unlike a host entry it carries nothing recoverable, and a `.corrupt`
        file nothing ever reaps is just litter on a public write path."""
        try:
            rec = json.loads(path.read_text())
            if not isinstance(rec, dict):
                raise ValueError("not an object")
            return rec
        except (json.JSONDecodeError, OSError, ValueError):
            path.unlink(missing_ok=True)
            return None

    def _sweep_challenges(self) -> list[Path]:
        """Drop expired/corrupt challenges; return what is still live.

        `*.claim` files are swept on the same rule: a crash between the rename
        that claims a nonce and the unlink that spends it would otherwise
        strand one forever, and a claim past its own expiry can never be
        spent by anyone.
        """
        now = self._clock()
        live = []
        for path in sorted(self._challenges.glob("*")):
            if path.suffix not in (".json", ".claim"):
                continue
            rec = self._read_challenge(path)
            if rec is None or now >= _as_float(rec.get("expires_at")):
                path.unlink(missing_ok=True)
            elif path.suffix == ".json":
                live.append(path)                    # a claim is already spent
        return live

    def issue_challenge(self) -> dict:
        """Mint a single-use nonce, stamped and expiring on the relay clock.

        Capped: this route is public and unauthenticated, and each call writes a
        file, so the store — not the HTTP layer — is the security boundary
        (`RequestStore.create`'s `max_pending` precedent). The cap is per *live*
        challenge, so it only ever bites on a flood inside one TTL window.
        """
        if len(self._sweep_challenges()) >= self._max_challenges:
            raise ChallengeCapReached("too many outstanding challenges; try later")
        now = self._clock()
        rec = {
            "nonce": secrets.token_hex(16),
            "issued_at": now,
            "expires_at": now + self._challenge_ttl,
        }
        self._write_atomic(self._challenges / f"{rec['nonce']}.json", rec)
        return rec

    def _consume_nonce(self, nonce: str) -> None:
        """Spend a nonce, or refuse.

        Single use is enforced by *claiming* the file with `os.rename` before
        reading it: rename is atomic and fails with FileNotFoundError for every
        loser, so two concurrent registrations quoting one nonce cannot both
        proceed (an exists()-read-unlink sequence would let both through — the
        enroll app serves sync endpoints on a threadpool, so this is reachable
        in one process).
        """
        if not _NONCE_RE.match(nonce or ""):
            raise ChallengeRefused(host_contract.MALFORMED_NONCE_DETAIL)
        path = self._challenges / f"{nonce}.json"
        claim = self._challenges / f"{nonce}.{secrets.token_hex(8)}.claim"
        try:
            os.rename(path, claim)
        except OSError:
            raise ChallengeRefused(host_contract.UNKNOWN_NONCE_DETAIL) from None
        try:
            rec = self._read_challenge(claim)
        finally:
            claim.unlink(missing_ok=True)
        if rec is None:
            raise ChallengeRefused(host_contract.UNKNOWN_NONCE_DETAIL)
        if self._clock() >= _as_float(rec.get("expires_at")):
            raise ChallengeRefused(host_contract.EXPIRED_CHALLENGE_DETAIL)

    # --- registration -------------------------------------------------------

    def _entry_path(self, host_id: str) -> Path:
        if not _HOST_ID_RE.match(host_id or ""):
            raise InvalidHostId(host_id)
        return self._hosts / f"{host_id}.json"

    def register(self, payload: dict, *, nonce: str, signature: str) -> dict:
        """Verify and publish one host's registration.

        Order matters and is the invariant: id shape → nonce → signature →
        arbitration → write. Nothing below the signature check can be reached
        by an unsigned request, and nothing writes before arbitration.
        """
        host_id = str(payload.get("host_id", ""))
        path = self._entry_path(host_id)           # traversal dies here
        self._consume_nonce(nonce)

        identity = str(payload.get("key_fingerprint", ""))
        blob = host_contract.signing_blob(nonce, payload)
        if not self._verify(
            blob,
            signature=signature,
            identity=identity,
            allowed_signers=self._allowed_signers(),
            namespace=host_contract.NAMESPACE,
        ):
            raise SignatureRefused(host_contract.UNAPPROVED_KEY_DETAIL)

        now = self._clock()
        incumbent = self._read_rec(path) if path.exists() else None
        previous_instance_id = None
        if incumbent is not None and not self._same_identity(incumbent, payload):
            self._arbitrate(incumbent, payload, now)
            previous_instance_id = incumbent.get("instance_id")

        entry = {
            "host_id": host_id,
            **{f: payload.get(f) for f in _PAYLOAD_FIELDS},
            "first_seen": incumbent.get("first_seen", now) if incumbent else now,
            "last_seen": now,                       # relay clock, never the payload's
            # Carried forward on a heartbeat: the takeover is a fact about the
            # entry, and the next re-registration must not erase the audit of
            # who used to hold it.
            "previous_instance_id": (
                previous_instance_id
                if incumbent is None or previous_instance_id is not None
                else incumbent.get("previous_instance_id")
            ),
        }
        self._write_atomic(path, entry)
        return entry

    @staticmethod
    def _same_identity(incumbent: dict, payload: dict) -> bool:
        """AC11: an identical `(key fp, machine fp, instance_id)` re-post is the
        same daemon reconnecting — idempotent, not contention."""
        return all(
            incumbent.get(f) == payload.get(f)
            for f in ("key_fingerprint", "machine_fingerprint", "instance_id")
        )

    def _arbitrate(self, incumbent: dict, payload: dict, now: float) -> None:
        """Refuse the claim iff the incumbent is still live and still itself.

        A stale entry is nobody's: no probe, straight takeover. Otherwise ask
        the incumbent's published URL who it is — the only answer that can
        refuse a claimant is the incumbent's own `instance_id`. An unreachable
        or unrecognisable incumbent, or one already answering as the claimant
        (a restart whose old tunnel is gone), yields the entry.
        """
        if now - _as_float(incumbent.get("last_seen")) >= self._stale_after:
            return
        public_url = incumbent.get("public_url") or ""
        try:
            answered = self._probe(public_url) if public_url else None
        except Exception:                            # timeout, DNS, TLS, refused
            answered = None
        if answered is not None and answered == incumbent.get("instance_id"):
            raise DuplicateIdentity(_REIDENTIFY_HINT)

    # --- reads --------------------------------------------------------------

    def get_host(self, host_id: str) -> dict | None:
        try:
            path = self._entry_path(host_id)
        except InvalidHostId:
            return None
        return self._read_rec(path) if path.exists() else None

    def _entries(self) -> list[dict]:
        out = []
        for path in sorted(self._hosts.glob("*.json")):
            rec = self._read_rec(path)
            if rec is not None:
                out.append(rec)
        return out

    def list_hosts(self, pending: Sequence[dict] = ()) -> list[dict]:
        """Registered hosts (`online`/`offline` by the relay clock) followed by
        unapproved enroll requests as `pending-approval`, so a freshly
        provisioned VM is visible on the phone before anyone approves it (AC1).
        """
        now = self._clock()
        hosts = []
        known_keys = set()
        for rec in self._entries():
            known_keys.add(rec.get("key_fingerprint"))
            stale = now - _as_float(rec.get("last_seen")) >= self._stale_after
            hosts.append({**rec, "state": "offline" if stale else "online"})
        for req in pending:
            if req.get("fingerprint") in known_keys:
                continue                             # already registered
            hosts.append({
                "host_id": None,
                "state": "pending-approval",
                "label": req.get("hostname") or "",
                "key_fingerprint": req.get("fingerprint"),
                "request_id": req.get("id"),
                "created_at": req.get("created_at"),
            })
        return hosts
