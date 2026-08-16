"""Single-instance lease: the held flock is the liveness authority; the
recorded pid is diagnostic only. Cross-process cases use real multiprocessing
(the `tests/core/test_store_concurrency.py` precedent) because flock is
per-open-file-description — a second acquire in the SAME process would succeed
and prove nothing.
"""
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from mship.core.daemon.lease import DaemonLease, LeaseInfo


def _acquire_and_report(path: str, q: multiprocessing.Queue, hold_s: float = 0.0):
    lease = DaemonLease(Path(path))
    holder = lease.try_acquire(version="1.0", socket_path="/tmp/x.sock")
    if holder is None:
        q.put(("won", os.getpid()))
        if hold_s:
            import time

            time.sleep(hold_s)
    else:
        q.put(("lost", holder.pid))


def _spawn(target, *args):
    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=target, args=args)
    p.start()
    return p


def test_acquire_creates_lease_and_holds_flock(tmp_path: Path):
    path = tmp_path / "daemon.lease"
    lease = DaemonLease(path)
    assert lease.try_acquire(version="0.5.52", socket_path="/run/s.sock") is None
    raw = json.loads(path.read_text())
    assert raw["pid"] == os.getpid()
    assert raw["version"] == "0.5.52"
    assert raw["socket_path"] == "/run/s.sock"
    assert raw["started_at"]

    # A child process must lose and see this process as the holder.
    q = multiprocessing.get_context("spawn").Queue()
    p = _spawn(_acquire_and_report, str(path), q)
    outcome, pid = q.get(timeout=30)
    p.join(timeout=30)
    assert outcome == "lost"
    assert pid == os.getpid()
    lease.release()


def test_late_arrival_after_write_loses(tmp_path: Path):
    """Winner acquires AND completes its JSON write; a late arrival must lose.

    Deterministically catches the inode-swap failure mode: any tmp+os.replace
    write would leave the winner's flock on an unlinked inode, so the late
    arrival would flock the fresh file and 'win'. This is also the steady-state
    `daemon run`-while-unit-is-up case.
    """
    path = tmp_path / "daemon.lease"
    lease = DaemonLease(path)
    assert lease.try_acquire(version="1", socket_path="/s") is None  # write completed

    q = multiprocessing.get_context("spawn").Queue()
    p = _spawn(_acquire_and_report, str(path), q)
    outcome, _pid = q.get(timeout=30)
    p.join(timeout=30)
    assert outcome == "lost"
    lease.release()


def test_stale_lease_dead_pid_is_reclaimed(tmp_path: Path):
    path = tmp_path / "daemon.lease"
    path.write_text(
        json.dumps(
            {"pid": 2**22 + 1, "started_at": "2026-01-01T00:00:00+00:00", "version": "0", "socket_path": "/s"}
        )
    )
    lease = DaemonLease(path)
    assert lease.try_acquire(version="1", socket_path="/s2") is None
    assert json.loads(path.read_text())["pid"] == os.getpid()
    lease.release()


def test_pid_reuse_does_not_read_as_running(tmp_path: Path):
    """A lease pointing at a LIVE but unrelated process (our parent) with no
    flock held is still reclaimed: the flock is the authority, not the pid."""
    path = tmp_path / "daemon.lease"
    live_unrelated = os.getppid()
    path.write_text(
        json.dumps(
            {"pid": live_unrelated, "started_at": "2026-01-01T00:00:00+00:00", "version": "0", "socket_path": "/s"}
        )
    )
    lease = DaemonLease(path)
    assert lease.try_acquire(version="1", socket_path="/s2") is None
    lease.release()


def test_concurrent_cold_start_converges(tmp_path: Path):
    path = tmp_path / "daemon.lease"
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    procs = [_spawn(_acquire_and_report, str(path), q, 2.0) for _ in range(5)]
    outcomes = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
    wins = [o for o in outcomes if o[0] == "won"]
    losses = [o for o in outcomes if o[0] == "lost"]
    assert len(wins) == 1
    assert len(losses) == 4
    # Losers report the winner's pid or None ("held by unknown" mid-write race);
    # never a second win.
    winner_pid = wins[0][1]
    assert all(pid in (winner_pid, None) for _, pid in losses)


def test_loser_mid_write_returns_unknown_holder(tmp_path: Path):
    """Flock held but the record unreadable after retries → LeaseInfo(pid=None)."""
    path = tmp_path / "daemon.lease"
    lease = DaemonLease(path)
    assert lease.try_acquire(version="1", socket_path="/s") is None
    # Simulate an in-progress write: truncate the record through the fd.
    os.ftruncate(lease._fd, 0)

    q = multiprocessing.get_context("spawn").Queue()
    p = _spawn(_acquire_and_report, str(path), q)
    outcome, pid = q.get(timeout=30)
    p.join(timeout=30)
    assert outcome == "lost"
    assert pid is None
    lease.release()


def test_release_allows_reacquire(tmp_path: Path):
    path = tmp_path / "daemon.lease"
    lease = DaemonLease(path)
    assert lease.try_acquire(version="1", socket_path="/s") is None
    lease.release()
    lease2 = DaemonLease(path)
    assert lease2.try_acquire(version="2", socket_path="/s") is None
    lease2.release()
