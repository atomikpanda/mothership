"""Tests for the git-backed run-state store (claim + run-log) on an orphan ref.

The "origin" is a local bare repo (mirrors tests/util/test_git.py fixtures); each
RunStateRepo gets its own workdir so we exercise real clone/fetch/push sharing
through git rather than a shared in-process object.
"""
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
