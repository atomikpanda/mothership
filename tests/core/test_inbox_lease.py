from datetime import datetime, timedelta, timezone

from mship.core.inbox_lease import InboxLease

T0 = datetime(2026, 7, 8, 1, 0, 0, tzinfo=timezone.utc)


def _lease(tmp_path, ttl=10.0, alive=True):
    return InboxLease(
        tmp_path / "inbox-listener.lock",
        ttl_seconds=ttl,
        pid_alive=lambda pid: alive,
    )


def test_acquire_on_empty_succeeds(tmp_path):
    lease = _lease(tmp_path)
    assert lease.try_acquire(pid=100, now=T0) is None
    info = lease.read()
    assert info is not None and info.pid == 100


def test_second_live_holder_is_refused(tmp_path):
    lease = _lease(tmp_path, alive=True)
    assert lease.try_acquire(pid=100, now=T0) is None
    # a different pid, holder still alive + heartbeat fresh → refused
    holder = lease.try_acquire(pid=200, now=T0 + timedelta(seconds=1))
    assert holder is not None and holder.pid == 100


def test_same_pid_reacquire_is_idempotent(tmp_path):
    lease = _lease(tmp_path)
    lease.try_acquire(pid=100, now=T0)
    assert lease.try_acquire(pid=100, now=T0 + timedelta(seconds=1)) is None


def test_reclaims_when_holder_dead(tmp_path):
    dead = InboxLease(tmp_path / "inbox-listener.lock", ttl_seconds=10.0,
                      pid_alive=lambda pid: False)
    dead.try_acquire(pid=100, now=T0)
    # holder pid not alive → a new pid reclaims even with a fresh heartbeat
    assert dead.try_acquire(pid=200, now=T0 + timedelta(seconds=1)) is None
    assert dead.read().pid == 200


def test_reclaims_when_heartbeat_stale(tmp_path):
    lease = _lease(tmp_path, ttl=10.0, alive=True)
    lease.try_acquire(pid=100, now=T0)
    # heartbeat older than TTL → reclaimable even though pid is "alive"
    assert lease.try_acquire(pid=200, now=T0 + timedelta(seconds=11)) is None
    assert lease.read().pid == 200


def test_refresh_advances_heartbeat_only_for_holder(tmp_path):
    lease = _lease(tmp_path)
    lease.try_acquire(pid=100, now=T0)
    lease.refresh(pid=100, now=T0 + timedelta(seconds=5))
    assert lease.read().heartbeat_at == T0 + timedelta(seconds=5)
    # a non-holder refresh must not steal the lease
    lease.refresh(pid=999, now=T0 + timedelta(seconds=6))
    assert lease.read().pid == 100


def test_release_removes_only_own_lease(tmp_path):
    lease = _lease(tmp_path)
    lease.try_acquire(pid=100, now=T0)
    lease.release(pid=999)           # not the holder → no-op
    assert lease.read() is not None
    lease.release(pid=100)           # holder → removed
    assert lease.read() is None


def test_corrupt_lease_file_treated_as_unheld(tmp_path):
    path = tmp_path / "inbox-listener.lock"
    path.write_text("{not json")
    lease = _lease(tmp_path)
    assert lease.read() is None
    assert lease.try_acquire(pid=100, now=T0) is None
