"""Short-lived host bearer tokens (#471 Task 2) and the single expiry owner.

The host mints these and the host verifies them, so the only clock in the
bearer loop is this machine's — which makes a *wall-clock step on this VM* the
one residual hazard (AC10). Every test here pins a behavior that a bare
`clock() >= expires_at` predicate would get wrong.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mship.core.daemon.host_token import (
    HOST_TOKEN_TTL_S,
    MAX_HOST_TOKENS,
    ensure_host_root_secret,
    issue_host_token,
    verify_host_token,
)
from mship.core.daemon.paths import (
    daemon_state_dir,
    host_secret_path,
    host_tokens_path,
)
from mship.core.relay import keys as relay_keys
from mship.core.relay.token_clock import SKEW_SECONDS, AnchoredClock, boot_epoch


class _Clock:
    """A fake wall+monotonic pair (the `test_health.py` fake-clock style).

    `advance` is real time passing (both hands move); `step` is a wall-clock
    jump (an NTP correction — the monotonic hand does not move).
    """

    def __init__(
        self,
        wall: float = 1_700_000_000.0,
        mono: float = 10.0,
        epoch: str = "boot:aaaa",
    ):
        self.wall = wall
        self.mono = mono
        self.epoch = epoch

    def anchored(self) -> AnchoredClock:
        return AnchoredClock(
            wall=lambda: self.wall, mono=lambda: self.mono, epoch=self.epoch
        )

    def advance(self, dt: float) -> None:
        self.wall += dt
        self.mono += dt

    def step(self, dt: float) -> None:
        self.wall += dt


def _records(home: Path) -> dict:
    return json.loads(host_tokens_path(home).read_text())["tokens"]


# --- the per-host root secret (mirrors keys.ensure_subdomain_secret) --------


def test_root_secret_is_32_bytes_0600_and_stable(tmp_path: Path):
    s1 = ensure_host_root_secret(tmp_path)
    assert isinstance(s1, bytes) and len(s1) == 32
    path = host_secret_path(tmp_path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert ensure_host_root_secret(tmp_path) == s1  # idempotent


def test_root_secret_regenerates_truncated_file(tmp_path: Path):
    path = host_secret_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"short")
    s = ensure_host_root_secret(tmp_path)
    assert len(s) == 32
    assert path.read_bytes() == s
    assert ensure_host_root_secret(tmp_path) == s  # now stable


def test_root_secret_adopts_the_winner_of_a_creation_race(tmp_path: Path, monkeypatch):
    """Two daemons racing to create it must end up with the SAME secret: the
    loser adopts the winner's bytes rather than crashing or overwriting."""
    path = host_secret_path(tmp_path)
    winner = b"W" * 32
    real_open = relay_keys.os.open

    def racing_open(target, flags, mode=0o777):
        if Path(target) != path:
            return real_open(target, flags, mode)
        # The winner lands its bytes between our unlink and our O_EXCL create.
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(winner)
        monkeypatch.setattr(relay_keys.os, "open", real_open)
        raise FileExistsError()

    monkeypatch.setattr(relay_keys.os, "open", racing_open)
    assert ensure_host_root_secret(tmp_path) == winner
    assert path.read_bytes() == winner


# --- issuance: hash-at-rest plus an epoch-tagged monotonic floor -----------


def test_issue_persists_only_the_hash_expiry_and_epoch_tagged_floor(tmp_path: Path):
    clk = _Clock()
    token = issue_host_token(tmp_path, ttl_seconds=300, clock=clk.anchored())
    token_id, secret = token.split(".", 1)

    raw = host_tokens_path(tmp_path).read_text()
    assert secret not in raw  # plaintext is never re-derivable from disk

    rec = _records(tmp_path)[token_id]
    assert set(rec) == {
        "token_id",
        "secret_hash",
        "expires_at",
        "mono_deadline",
        "epoch",
    }
    assert rec["expires_at"] == clk.wall + 300
    assert rec["mono_deadline"] == clk.mono + 300
    assert rec["epoch"] == clk.epoch  # the floor is useless untagged


def test_default_ttl_is_short(tmp_path: Path):
    assert HOST_TOKEN_TTL_S <= 900
    clk = _Clock()
    issue_host_token(tmp_path, clock=clk.anchored())
    rec = next(iter(_records(tmp_path).values()))
    assert rec["expires_at"] == clk.wall + HOST_TOKEN_TTL_S


def test_verify_accepts_before_expiry(tmp_path: Path):
    clk = _Clock()
    token = issue_host_token(tmp_path, ttl_seconds=300, clock=clk.anchored())
    clk.advance(299)
    ht = verify_host_token(tmp_path, token, clock=clk.anchored())
    assert ht is not None
    assert ht.token_id == token.split(".", 1)[0]


def test_verify_rejects_a_wrong_secret(tmp_path: Path):
    clk = _Clock()
    token = issue_host_token(tmp_path, ttl_seconds=300, clock=clk.anchored())
    token_id = token.split(".", 1)[0]
    assert (
        verify_host_token(tmp_path, f"{token_id}.wrong", clock=clk.anchored()) is None
    )


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "no-dot",
        ".",
        "abc.",
        "../../../etc/passwd.x",
        "..%2f..%2fx.y",
        "DEADBEEF.s",
        "z" * 40 + ".s",
        "deadbeef.secret",
    ],
)
def test_verify_never_raises_on_hostile_input(tmp_path: Path, presented: str):
    issue_host_token(tmp_path, clock=_Clock().anchored())
    assert verify_host_token(tmp_path, presented, clock=_Clock().anchored()) is None


def test_verify_never_raises_on_a_corrupt_store(tmp_path: Path):
    clk = _Clock()
    token = issue_host_token(tmp_path, clock=clk.anchored())
    host_tokens_path(tmp_path).write_text("{not json")
    assert verify_host_token(tmp_path, token, clock=clk.anchored()) is None


@pytest.mark.parametrize("field", ["expires_at", "mono_deadline"])
def test_verify_rejects_an_oversized_persisted_deadline(
    tmp_path: Path, field: str
):
    clk = _Clock()
    token = issue_host_token(tmp_path, clock=clk.anchored())
    path = host_tokens_path(tmp_path)
    doc = json.loads(path.read_text())
    next(iter(doc["tokens"].values()))[field] = 10**400
    path.write_text(json.dumps(doc))

    assert verify_host_token(tmp_path, token, clock=clk.anchored()) is None


# --- restart safety: a persisted monotonic floor must carry its epoch ------


def test_survives_a_restart_under_a_new_monotonic_origin_and_epoch(tmp_path: Path):
    """#470 supervises the daemon and #473's upgrades restart it, so a restart
    is the NORMAL case. A bare persisted `time.monotonic()` floor would compare
    two boot epochs and either expire everything or nothing."""
    minted = _Clock(wall=1_700_000_000.0, mono=10.0, epoch="boot:aaaa")
    token = issue_host_token(tmp_path, ttl_seconds=300, clock=minted.anchored())

    # Rebooted: monotonic restarted from a wildly different origin, new epoch.
    fresh = _Clock(wall=minted.wall + 100, mono=500_000.0, epoch="boot:bbbb")
    assert verify_host_token(tmp_path, token, clock=fresh.anchored()) is not None

    stale = _Clock(
        wall=minted.wall + 300 + SKEW_SECONDS + 1, mono=1.0, epoch="boot:bbbb"
    )
    assert verify_host_token(tmp_path, token, clock=stale.anchored()) is None


# --- AC10: a wall-clock step must not extend (or silently lengthen) a token -


def test_backwards_wall_step_does_not_extend_a_token(tmp_path: Path):
    """AC10, the real bound: after a one-hour BACKWARDS step the token still
    dies once 300s of monotonic time have elapsed. `clock() >= expires_at`
    would have handed the bearer another hour of life."""
    clk = _Clock()
    token = issue_host_token(tmp_path, ttl_seconds=300, clock=clk.anchored())

    clk.step(-3600)
    clk.advance(299)
    assert verify_host_token(tmp_path, token, clock=clk.anchored()) is not None
    clk.advance(1)
    assert verify_host_token(tmp_path, token, clock=clk.anchored()) is None


def test_forwards_wall_step_past_expiry_plus_skew_rejects(tmp_path: Path):
    clk = _Clock()
    token = issue_host_token(tmp_path, ttl_seconds=300, clock=clk.anchored())
    clk.step(300 + SKEW_SECONDS + 1)
    assert verify_host_token(tmp_path, token, clock=clk.anchored()) is None


def test_skew_grace_does_not_lengthen_an_ordinary_token(tmp_path: Path):
    """The grace is for a DETECTED discontinuity only — with both hands moving
    together the token dies exactly at its TTL, not at TTL + skew."""
    clk = _Clock()
    token = issue_host_token(tmp_path, ttl_seconds=300, clock=clk.anchored())
    clk.advance(300)
    assert verify_host_token(tmp_path, token, clock=clk.anchored()) is None


# --- the store cannot grow without bound -----------------------------------


def test_issuing_prunes_expired_records(tmp_path: Path):
    clk = _Clock()
    for _ in range(5):
        issue_host_token(tmp_path, ttl_seconds=10, clock=clk.anchored())
    assert len(_records(tmp_path)) == 5
    clk.advance(10 + SKEW_SECONDS + 1)
    issue_host_token(tmp_path, ttl_seconds=10, clock=clk.anchored())
    assert len(_records(tmp_path)) == 1


def test_issuing_caps_the_number_of_live_records(tmp_path: Path):
    clk = _Clock()
    for _ in range(MAX_HOST_TOKENS + 3):
        issue_host_token(tmp_path, ttl_seconds=10_000, clock=clk.anchored())
        clk.advance(1)  # distinct expiries, so "drop the oldest" is well defined
    assert len(_records(tmp_path)) == MAX_HOST_TOKENS


def test_a_shorter_lived_token_minted_at_the_cap_still_verifies(tmp_path: Path):
    """The cap must never evict the credential it is handing back. Evicting
    'the soonest to expire' AFTER inserting picks the new token whenever it has
    the shorter TTL — it would be issued already dead."""
    clk = _Clock()
    for _ in range(MAX_HOST_TOKENS):
        issue_host_token(tmp_path, ttl_seconds=10_000, clock=clk.anchored())
    short = issue_host_token(tmp_path, ttl_seconds=60, clock=clk.anchored())

    assert verify_host_token(tmp_path, short, clock=clk.anchored()) is not None
    assert len(_records(tmp_path)) == MAX_HOST_TOKENS
    assert short.split(".", 1)[0] in _records(tmp_path)


def test_every_issued_token_verifies_while_the_cap_churns(tmp_path: Path):
    clk = _Clock()
    for i in range(MAX_HOST_TOKENS + 10):
        # Alternate long and short TTLs so the new record lands on both sides
        # of the eviction ordering.
        token = issue_host_token(
            tmp_path, ttl_seconds=60 if i % 2 else 10_000, clock=clk.anchored()
        )
        assert verify_host_token(tmp_path, token, clock=clk.anchored()) is not None


def test_verify_writes_nothing(tmp_path: Path):
    """AC11's "reconnect performs no writes" starts here: verification is a
    pure read, so a flapping phone cannot churn the store."""
    clk = _Clock()
    token = issue_host_token(tmp_path, ttl_seconds=300, clock=clk.anchored())
    before = host_tokens_path(tmp_path).read_bytes()
    for _ in range(5):
        assert verify_host_token(tmp_path, token, clock=clk.anchored()) is not None
    assert host_tokens_path(tmp_path).read_bytes() == before


# --- the state dir is owner-only, whatever the umask -----------------------


@pytest.fixture
def loose_umask():
    """A permissive umask, so these assertions test the corrective chmod rather
    than the ambient umask (mkdir's mode argument is umask-masked)."""
    previous = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


@pytest.mark.parametrize(
    "entry_point",
    [
        lambda home: ensure_host_root_secret(home),
        lambda home: issue_host_token(home, clock=_Clock().anchored()),
        lambda home: verify_host_token(
            home, "deadbeefdeadbeef.s", clock=_Clock().anchored()
        ),
    ],
)
def test_state_dir_is_owner_only_after_any_entry_point(
    tmp_path: Path, loose_umask, entry_point
):
    entry_point(tmp_path)
    assert _mode(daemon_state_dir(tmp_path)) == "0o700"


def test_a_pre_existing_loose_state_dir_is_tightened(tmp_path: Path, loose_umask):
    """`exist_ok=True` never fixes a dir that is already too loose — only the
    corrective chmod does."""
    state_dir = daemon_state_dir(tmp_path)
    state_dir.mkdir(parents=True)
    state_dir.chmod(0o755)
    ensure_host_root_secret(tmp_path)
    assert _mode(state_dir) == "0o700"


def test_the_token_file_is_owner_only(tmp_path: Path, loose_umask):
    issue_host_token(tmp_path, clock=_Clock().anchored())
    assert _mode(host_tokens_path(tmp_path)) == "0o600"


# --- the epoch tag itself --------------------------------------------------


def test_boot_epoch_prefers_the_kernel_boot_id():
    assert boot_epoch(reader=lambda: "c0ffee-1234\n") == "boot:c0ffee-1234"


def test_boot_epoch_falls_back_when_no_boot_id_is_readable():
    def missing():
        raise OSError("no /proc")

    fallback = boot_epoch(reader=missing)
    assert fallback and not fallback.startswith("boot:")
    # Stable within a process (so a same-process check still uses monotonic)…
    assert boot_epoch(reader=missing) == fallback
    # …and it is a process anchor, so a restart cannot reuse another boot's floor.
    assert boot_epoch(reader=lambda: "") == fallback


def test_boot_epoch_of_two_boots_differ():
    assert boot_epoch(reader=lambda: "aaa") != boot_epoch(reader=lambda: "bbb")
