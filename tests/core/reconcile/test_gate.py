import time
from datetime import datetime, timezone
from pathlib import Path

from mship.core.state import Task, WorkspaceState
from mship.core.reconcile.cache import ReconcileCache, CachePayload
from mship.core.reconcile.detect import UpstreamState, PRSnapshot, GitSnapshot
from mship.core.reconcile.gate import (
    Decision, GateAction, reconcile_now, should_block,
)


def _task(slug: str, **over) -> Task:
    base = dict(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], worktrees={"r": Path("/tmp/fake") / slug},
        branch=f"feat/{slug}", base_branch="main",
    )
    base.update(over)
    return Task(**base)


def test_reconcile_now_uses_fresh_cache(tmp_path: Path):
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"}},
        ignored=[],
    ))
    state = WorkspaceState(tasks={"a": _task("a")})
    decisions = reconcile_now(state, cache=cache, fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("should not fetch")))
    assert decisions["a"].state == UpstreamState.merged


def test_reconcile_now_refetches_when_stale(tmp_path: Path):
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time() - 9999, ttl_seconds=300,
        results={"a": {"state": "in_sync"}}, ignored=[],
    ))
    state = WorkspaceState(tasks={"a": _task("a")})

    calls: list[list[str]] = []
    def fetcher(branches, worktrees):
        calls.append(list(branches))
        return (
            {"feat/a": PRSnapshot(head_ref="feat/a", state="MERGED", base_ref="main",
                                   merge_commit="x", url="https://x/pr/9", updated_at="z")},
            {"feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=0)},
        )
    decisions = reconcile_now(state, cache=cache, fetcher=fetcher)
    assert calls == [["feat/a"]]
    assert decisions["a"].state == UpstreamState.merged


def test_reconcile_now_falls_back_to_cache_on_fetcher_error(tmp_path: Path):
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time() - 9999, ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"}},
        ignored=[],
    ))
    state = WorkspaceState(tasks={"a": _task("a")})

    def bad_fetcher(*_):
        from mship.core.reconcile.fetch import FetchError
        raise FetchError("offline")

    decisions = reconcile_now(state, cache=cache, fetcher=bad_fetcher)
    assert decisions["a"].state == UpstreamState.merged


def test_reconcile_now_returns_unavailable_on_error_without_cache(tmp_path: Path):
    cache = ReconcileCache(tmp_path)
    state = WorkspaceState(tasks={"a": _task("a")})
    def bad_fetcher(*_):
        from mship.core.reconcile.fetch import FetchError
        raise FetchError("offline")
    decisions = reconcile_now(state, cache=cache, fetcher=bad_fetcher)
    assert decisions == {}


def test_should_block_merged_on_finish():
    d = Decision(slug="a", state=UpstreamState.merged, pr_url="u", pr_number=1,
                 base="main", merge_commit="x", updated_at="z")
    assert should_block(d, command="finish", ignored=[]) is GateAction.block


def test_should_block_merged_on_close_is_allowed():
    d = Decision(slug="a", state=UpstreamState.merged, pr_url="u", pr_number=1,
                 base="main", merge_commit="x", updated_at="z")
    assert should_block(d, command="close", ignored=[]) is GateAction.allow


def test_should_block_base_changed_on_precommit_is_allowed():
    d = Decision(slug="a", state=UpstreamState.base_changed, pr_url="u", pr_number=1,
                 base="develop", merge_commit=None, updated_at="z")
    assert should_block(d, command="precommit", ignored=[]) is GateAction.allow


def test_should_block_respects_ignore_list():
    d = Decision(slug="a", state=UpstreamState.merged, pr_url="u", pr_number=1,
                 base="main", merge_commit="x", updated_at="z")
    assert should_block(d, command="finish", ignored=["a"]) is GateAction.allow


def test_diverged_warns_on_spawn_blocks_on_finish():
    d = Decision(slug="a", state=UpstreamState.diverged, pr_url="u", pr_number=1,
                 base="main", merge_commit=None, updated_at="z")
    assert should_block(d, command="spawn", ignored=[]) is GateAction.warn
    assert should_block(d, command="finish", ignored=[]) is GateAction.block
