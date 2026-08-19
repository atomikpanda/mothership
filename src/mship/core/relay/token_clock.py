"""The single owner of "is this credential still expired?" (#471, AC10).

Two callers justify it: the relay's run tokens (`run_token.verify_run_token`)
and the daemon's host bearers (`core.daemon.host_token`). Both used to be — or
would have been — a bare `clock() >= expires_at`, which gets two things wrong:

- **A wall-clock step.** A long-lived VM's clock is corrected by NTP, sometimes
  by an hour. Stepped *backwards*, `clock() >= expires_at` silently extends
  every live bearer by that hour. The fix is a monotonic floor: real elapsed
  time, immune to corrections.
- **A monotonic floor without an epoch is worse than none.** `time.monotonic()`
  is only comparable within one boot (CPython's is boot-relative on Linux), and
  the daemon restarts routinely (#470 supervises it; #473's upgrades restart
  it). A floor persisted across a reboot compares two different origins and
  either expires everything or nothing. So each floor is stamped with the
  epoch it was taken in, and a floor from another epoch is simply not used.

Expiry is therefore the **earlier** of two bounds: the monotonic deadline (when
its epoch still matches) and the wall deadline plus a skew grace. The grace
never lengthens an ordinary token — the monotonic bound always fires first —
so it only takes effect where the anchor cannot vouch for elapsed time: a
cross-epoch (post-restart) check, or a caller with no anchor at all.
"""
from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

# Tolerance for wall-clock disagreement: an NTP step correction, or the offset
# between the machine that issued a token and the one presenting it. Kept well
# under the host bearer's TTL so that even the anchorless fallback path cannot
# come close to doubling a token's life.
SKEW_SECONDS = 120.0

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")

# Per-process anchor for platforms with no kernel boot id: it is stable for the
# life of this process (so same-process checks still use the monotonic bound)
# and unguessably different in the next one (so a restart cannot mistake
# another process's floor for its own).
_PROCESS_EPOCH = f"proc:{secrets.token_hex(8)}"

_epoch_cache: str | None = None


def _read_boot_id() -> str:
    return _BOOT_ID_PATH.read_text()


def boot_epoch(reader: Callable[[], str] = _read_boot_id) -> str:
    """An id for the epoch the current `time.monotonic()` origin belongs to."""
    try:
        value = reader().strip()
    except OSError:
        value = ""
    return f"boot:{value}" if value else _PROCESS_EPOCH


def current_epoch() -> str:
    """This process's epoch, resolved once (it cannot change while we run)."""
    global _epoch_cache
    if _epoch_cache is None:
        _epoch_cache = boot_epoch()
    return _epoch_cache


def is_expired(expires_at: float, now: float, *, skew: float = SKEW_SECONDS) -> bool:
    """Wall-clock expiry with a skew grace — the bound for callers with no
    monotonic anchor to appeal to. Invalid persisted deadlines fail closed."""
    return not math.isfinite(expires_at) or now >= expires_at + skew


@dataclass(frozen=True)
class Deadline:
    """When a credential dies, stated in both clocks plus the epoch that makes
    the monotonic half meaningful."""

    expires_at: float       # wall clock (what a human/API sees)
    mono_deadline: float    # monotonic, valid only within `epoch`
    epoch: str

    def as_record(self) -> dict:
        return {
            "expires_at": self.expires_at,
            "mono_deadline": self.mono_deadline,
            "epoch": self.epoch,
        }

    @classmethod
    def from_record(cls, rec: Mapping) -> "Deadline | None":
        """Read a persisted deadline, or None if it is unusable. An untagged or
        malformed floor is never guessed at — a missing epoch reads as "not
        ours", which falls back to the wall-clock bound."""
        try:
            expires_at = float(rec["expires_at"])
            mono_deadline = float(rec.get("mono_deadline", 0.0))
        except (KeyError, TypeError, ValueError):
            return None
        epoch = rec.get("epoch")
        return cls(
            expires_at=expires_at,
            mono_deadline=mono_deadline,
            epoch=epoch if isinstance(epoch, str) else "",
        )


class AnchoredClock:
    """Wall time anchored to `time.monotonic()`, tagged with its epoch.

    Injectable in whole (`wall`, `mono`, `epoch`) so tests can step either hand
    independently and simulate a reboot without sleeping or rebooting.
    """

    def __init__(
        self,
        *,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        epoch: str | None = None,
    ) -> None:
        self._wall = wall
        self._mono = mono
        self._epoch = current_epoch() if epoch is None else epoch

    @property
    def epoch(self) -> str:
        return self._epoch

    def deadline(self, ttl_seconds: float) -> Deadline:
        return Deadline(
            expires_at=self._wall() + ttl_seconds,
            mono_deadline=self._mono() + ttl_seconds,
            epoch=self._epoch,
        )

    def is_expired(self, deadline: Deadline | None) -> bool:
        """True if `deadline` has passed on either bound (fail closed: an
        unreadable deadline is expired)."""
        if deadline is None:
            return True
        if deadline.epoch == self._epoch and self._mono() >= deadline.mono_deadline:
            return True
        return is_expired(deadline.expires_at, self._wall())
