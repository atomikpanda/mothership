"""Tests for the git-backed run-state store (claim + run-log) on an orphan ref.

The "origin" is a local bare repo (mirrors tests/util/test_git.py fixtures); each
RunStateRepo gets its own workdir so we exercise real clone/fetch/push sharing
through git rather than a shared in-process object.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from mship.core.run_state import RunStateRepo

T0 = datetime(2026, 7, 8, tzinfo=timezone.utc)


@pytest.fixture
def tmp_origin(tmp_path):
    """A bare git repo standing in for the workspace repo's origin."""
    import subprocess

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    return origin


def _repo(tmp_origin, tmp_path, name, **kw):
    return RunStateRepo(tmp_origin, workdir=tmp_path / name, **kw)


def test_claim_is_exclusive_and_reclaimable(tmp_origin, tmp_path):
    a = _repo(tmp_origin, tmp_path, "a", ttl_seconds=30)
    b = _repo(tmp_origin, tmp_path, "b", ttl_seconds=30)

    assert a.try_claim("wi-1", holder="runA", now=T0) is None            # A wins
    refused = b.try_claim("wi-1", holder="runB", now=T0 + timedelta(seconds=1))
    assert refused is not None and refused.holder == "runA"              # B refused (live)

    # stale reclaim: past TTL, B can take over
    assert b.try_claim("wi-1", holder="runB", now=T0 + timedelta(seconds=31)) is None

    # a displaced holder (runA) cannot release runB's claim -> no-op
    a.release("wi-1", holder="runA")
    assert a.read_claim("wi-1").holder == "runB"


def test_same_holder_reclaim_is_idempotent(tmp_origin, tmp_path):
    a = _repo(tmp_origin, tmp_path, "a", ttl_seconds=30)
    assert a.try_claim("wi-1", holder="runA", now=T0) is None
    # same holder re-claiming while fresh succeeds (idempotent refresh-claim)
    assert a.try_claim("wi-1", holder="runA", now=T0 + timedelta(seconds=1)) is None
    assert a.read_claim("wi-1").holder == "runA"


def test_release_by_holder_frees_the_claim(tmp_origin, tmp_path):
    a = _repo(tmp_origin, tmp_path, "a", ttl_seconds=30)
    b = _repo(tmp_origin, tmp_path, "b", ttl_seconds=30)
    assert a.try_claim("wi-1", holder="runA", now=T0) is None
    a.release("wi-1", holder="runA")                 # holder releases
    assert a.read_claim("wi-1") is None
    # a fresh runner (fresh clone) can now claim the freed item
    assert b.try_claim("wi-1", holder="runB", now=T0 + timedelta(seconds=1)) is None


def test_refresh_advances_heartbeat_only_for_holder(tmp_origin, tmp_path):
    a = _repo(tmp_origin, tmp_path, "a", ttl_seconds=30)
    assert a.try_claim("wi-1", holder="runA", now=T0) is None
    a.refresh("wi-1", holder="runA", now=T0 + timedelta(seconds=20))
    # heartbeat advanced: at T0+40 it is only 20s old (< 30 ttl) so still live
    b = _repo(tmp_origin, tmp_path, "b", ttl_seconds=30)
    refused = b.try_claim("wi-1", holder="runB", now=T0 + timedelta(seconds=40))
    assert refused is not None and refused.holder == "runA"
    # a non-holder refresh must not steal the claim
    a.refresh("wi-1", holder="someone-else", now=T0 + timedelta(seconds=41))
    assert a.read_claim("wi-1").holder == "runA"


def test_default_ttl_keeps_long_run_claimed_past_old_ttl(tmp_origin, tmp_path):
    """FIX#3a: the default TTL is 4h, so an overnight run isn't reclaimed mid-flight.
    A claim 2h old (> the old 1800s TTL, < the new 14400s) is still live."""
    a = _repo(tmp_origin, tmp_path, "a")               # default ttl (now 4h)
    assert a.try_claim("wi-1", holder="runA", now=T0) is None
    b = _repo(tmp_origin, tmp_path, "b")
    refused = b.try_claim("wi-1", holder="runB", now=T0 + timedelta(hours=2))
    assert refused is not None and refused.holder == "runA"   # still held, not reclaimed


def test_cross_process_heartbeat_via_recorded_holder(tmp_origin, tmp_path):
    """FIX#3b: `mship item heartbeat` runs in a separate process from run-next, so it
    advances the heartbeat by reading the RECORDED holder and refreshing as it. Here
    a fresh clone does exactly that; the claim then survives past the TTL."""
    a = _repo(tmp_origin, tmp_path, "a", ttl_seconds=30)
    assert a.try_claim("wi-1", holder="runA", now=T0) is None
    hb = _repo(tmp_origin, tmp_path, "hb", ttl_seconds=30)   # a different "process"
    holder = hb.read_claim("wi-1").holder
    hb.refresh("wi-1", holder, now=T0 + timedelta(seconds=25))
    # at T0+40 the heartbeat is 15s old (< 30 ttl) → still live despite the original
    # claim being 40s old (which without a heartbeat would be reclaimable)
    c = _repo(tmp_origin, tmp_path, "c", ttl_seconds=30)
    refused = c.try_claim("wi-1", holder="runC", now=T0 + timedelta(seconds=40))
    assert refused is not None and refused.holder == "runA"


def test_run_log_appends_and_persists(tmp_origin, tmp_path):
    r = _repo(tmp_origin, tmp_path, "a")
    r.append_log("wi-1", "run started", now=T0)
    r.append_log("wi-1", "bailed: fork on auth approach", now=T0 + timedelta(seconds=5))
    # a fresh clone sees the appended entries in order
    entries = _repo(tmp_origin, tmp_path, "b").read_log("wi-1")
    assert [e.text for e in entries] == ["run started", "bailed: fork on auth approach"]
    assert entries[0].at == T0


def test_read_log_empty_for_unknown_item(tmp_origin, tmp_path):
    assert _repo(tmp_origin, tmp_path, "a").read_log("nope") == []


def test_read_claim_none_for_unknown_item(tmp_origin, tmp_path):
    assert _repo(tmp_origin, tmp_path, "a").read_claim("nope") is None


def test_try_claim_stands_down_on_same_item_conflict(tmp_origin, tmp_path):
    """Deterministic same-item race: B syncs an empty base and is about to push its
    own claim when A claims first. B's push is a non-fast-forward, its rebase hits an
    add/add conflict on the claim file — B must STAND DOWN (return A's ClaimInfo),
    not raise. Contract: None == you won; ClaimInfo == someone else holds it."""
    a = _repo(tmp_origin, tmp_path, "a", ttl_seconds=30)
    b = _repo(tmp_origin, tmp_path, "b", ttl_seconds=30)

    real_push = b._push
    fired = {"n": 0}

    def racing_push(*args, **kwargs):
        # right before B's first push, A grabs the item -> forces B's non-ff + conflict
        if fired["n"] == 0:
            fired["n"] += 1
            assert a.try_claim("wi-1", holder="runA", now=T0) is None
        return real_push(*args, **kwargs)

    b._push = racing_push
    got = b.try_claim("wi-1", holder="runB", now=T0)
    assert got is not None and got.holder == "runA"          # B stood down, no exception
    assert _repo(tmp_origin, tmp_path, "c").read_claim("wi-1").holder == "runA"  # single claim


def test_concurrent_same_item_claim_has_single_winner(tmp_origin, tmp_path):
    """Two runs (separate workdirs, unrelated roots) race the same item. Exactly one
    gets None (winner); the other gets the winner's ClaimInfo — never an exception —
    and origin ends with a single claim."""
    import threading

    a = _repo(tmp_origin, tmp_path, "a", ttl_seconds=30)
    b = _repo(tmp_origin, tmp_path, "b", ttl_seconds=30)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def claim(repo, holder):
        barrier.wait()
        try:
            results[holder] = repo.try_claim("wi-1", holder=holder, now=T0)
        except Exception as exc:  # noqa: BLE001
            results[holder] = exc

    ta = threading.Thread(target=claim, args=(a, "runA"))
    tb = threading.Thread(target=claim, args=(b, "runB"))
    ta.start(); tb.start(); ta.join(); tb.join()

    assert not any(isinstance(v, Exception) for v in results.values()), results
    nones = [h for h, v in results.items() if v is None]
    infos = [(h, v) for h, v in results.items() if v is not None]
    assert len(nones) == 1 and len(infos) == 1
    winner = nones[0]
    assert infos[0][1].holder == winner                       # loser sees the winner
    assert _repo(tmp_origin, tmp_path, "c").read_claim("wi-1").holder == winner


def test_concurrent_different_items_both_succeed(tmp_origin, tmp_path):
    """Concurrent first-writers on unrelated roots but DIFFERENT items must both win
    (per-item files → the bounded fetch+rebase+re-push merges cleanly)."""
    import threading

    a = _repo(tmp_origin, tmp_path, "a", ttl_seconds=30)
    b = _repo(tmp_origin, tmp_path, "b", ttl_seconds=30)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def claim(repo, holder, item):
        barrier.wait()
        try:
            results[holder] = repo.try_claim(item, holder=holder, now=T0)
        except Exception as exc:  # noqa: BLE001
            results[holder] = exc

    ta = threading.Thread(target=claim, args=(a, "runA", "wi-1"))
    tb = threading.Thread(target=claim, args=(b, "runB", "wi-2"))
    ta.start(); tb.start(); ta.join(); tb.join()

    assert results == {"runA": None, "runB": None}, results     # both claimed, no exception
    c = _repo(tmp_origin, tmp_path, "c")
    assert c.read_claim("wi-1").holder == "runA"
    assert c.read_claim("wi-2").holder == "runB"


def test_read_log_skips_corrupt_line(tmp_origin, tmp_path):
    """A corrupt/partial JSONL line is skipped, mirroring read_claim's tolerance."""
    r = _repo(tmp_origin, tmp_path, "a")
    r.append_log("wi-1", "first", now=T0)
    # corrupt the persisted log directly on the ref, then commit+push the damage
    r._sync()
    log = r._log_path("wi-1")
    log.write_text(log.read_text() + "{not json\n" + json.dumps({"text": "second", "at": T0.isoformat()}) + "\n")
    r._commit_and_push("corrupt the log")
    entries = _repo(tmp_origin, tmp_path, "b").read_log("wi-1")
    assert [e.text for e in entries] == ["first", "second"]     # bad line dropped, good ones kept


def test_push_retries_on_non_fast_forward(tmp_origin, tmp_path):
    """A stale writer whose push is rejected (non-fast-forward) recovers via one
    pull --rebase + re-push. Per-item files mean the rebase merges cleanly."""
    from mship.core.run_state import ClaimInfo

    a = _repo(tmp_origin, tmp_path, "a", ttl_seconds=30)
    b = _repo(tmp_origin, tmp_path, "b", ttl_seconds=30)

    assert a.try_claim("wi-1", holder="runA", now=T0) is None   # branch created @ commit1
    b._sync()                                                   # b adopts commit1
    a.refresh("wi-1", holder="runA", now=T0 + timedelta(seconds=5))  # origin -> commit2

    # b, now stale at commit1, writes a DIFFERENT item and pushes -> non-ff -> retry
    b._write_claim("wi-2", ClaimInfo(holder="runB", heartbeat_at=T0 + timedelta(seconds=6)))
    b._commit_and_push("claim wi-2 by runB")

    fresh = _repo(tmp_origin, tmp_path, "c")
    assert fresh.read_claim("wi-1").holder == "runA"
    assert fresh.read_claim("wi-2").holder == "runB"
