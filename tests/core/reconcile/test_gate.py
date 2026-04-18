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


# --- finished_at plumbing (issue #36) ---


def test_decision_has_finished_at_from_state_fresh_fetch(tmp_path: Path):
    """reconcile_now populates Decision.finished_at from state.tasks."""
    finished = datetime(2026, 4, 18, 13, 20, 28, tzinfo=timezone.utc)
    state = WorkspaceState(tasks={"a": _task("a", finished_at=finished)})
    cache = ReconcileCache(tmp_path)  # empty

    def _fetcher(branches, wts):
        return (
            {"feat/a": PRSnapshot(head_ref="feat/a", state="MERGED", base_ref="main", merge_commit="abc", url="u", updated_at="2026-04-18T13:21:00Z")},
            {"feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=0)},
        )

    decisions = reconcile_now(state, cache=cache, fetcher=_fetcher)
    assert decisions["a"].finished_at == finished.isoformat()


def test_decision_finished_at_none_when_task_not_finished(tmp_path: Path):
    state = WorkspaceState(tasks={"a": _task("a")})  # finished_at default None
    cache = ReconcileCache(tmp_path)

    def _fetcher(branches, wts):
        return (
            {"feat/a": PRSnapshot(head_ref="feat/a", state="OPEN", base_ref="main", merge_commit=None, url="u", updated_at="2026-04-18T13:21:00Z")},
            {"feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=0)},
        )

    decisions = reconcile_now(state, cache=cache, fetcher=_fetcher)
    assert decisions["a"].finished_at is None


def test_decision_finished_at_populated_from_cache_hit(tmp_path: Path):
    """Cache-hit path still plumbs finished_at from live state, not cache."""
    finished = datetime(2026, 4, 18, 13, 20, 28, tzinfo=timezone.utc)
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"}},
        ignored=[],
    ))
    state = WorkspaceState(tasks={"a": _task("a", finished_at=finished)})

    decisions = reconcile_now(
        state, cache=cache,
        fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
    assert decisions["a"].state == UpstreamState.merged
    assert decisions["a"].finished_at == finished.isoformat()


def test_should_block_respects_ignore_list():
    d = Decision(slug="a", state=UpstreamState.merged, pr_url="u", pr_number=1,
                 base="main", merge_commit="x", updated_at="z")
    assert should_block(d, command="finish", ignored=["a"]) is GateAction.allow


def test_diverged_warns_on_spawn_blocks_on_finish():
    d = Decision(slug="a", state=UpstreamState.diverged, pr_url="u", pr_number=1,
                 base="main", merge_commit=None, updated_at="z")
    assert should_block(d, command="spawn", ignored=[]) is GateAction.warn
    assert should_block(d, command="finish", ignored=[]) is GateAction.block


# --- should_block settled-task auto-allow (issue #36) ---


def _dec(state: UpstreamState, finished_at: str | None = None, slug: str = "a") -> Decision:
    return Decision(
        slug=slug, state=state, pr_url=None, pr_number=None,
        base=None, merge_commit=None, updated_at=None,
        finished_at=finished_at,
    )


def test_should_block_merged_unfinished_finish_blocks():
    """Regression: merged without finished_at still blocks (existing matrix)."""
    d = _dec(UpstreamState.merged, finished_at=None)
    assert should_block(d, command="finish", ignored=[]) == GateAction.block


def test_should_block_merged_finished_finish_allows():
    """New: merged PR for a task with finished_at set — allow finish."""
    d = _dec(UpstreamState.merged, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="finish", ignored=[]) == GateAction.allow


def test_should_block_merged_finished_spawn_allows():
    d = _dec(UpstreamState.merged, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="spawn", ignored=[]) == GateAction.allow


def test_should_block_merged_finished_precommit_still_blocks():
    """Scope boundary: precommit keeps the matrix behavior."""
    d = _dec(UpstreamState.merged, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="precommit", ignored=[]) == GateAction.block


def test_should_block_merged_finished_close_allows():
    """Regression: close already allowed merged; settled logic is a no-op here."""
    d = _dec(UpstreamState.merged, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="close", ignored=[]) == GateAction.allow


def test_should_block_closed_finished_finish_allows():
    """Closed PRs with finished_at also settle."""
    d = _dec(UpstreamState.closed, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="finish", ignored=[]) == GateAction.allow


def test_should_block_in_sync_finished_unchanged():
    """finished_at set but state=in_sync → matrix applies (finish allows here)."""
    d = _dec(UpstreamState.in_sync, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="finish", ignored=[]) == GateAction.allow


def test_should_block_diverged_finished_still_blocks():
    """Regression: diverged state still blocks even if finished_at is set.
    A merged-then-local-commits-upstream situation is not 'settled'."""
    d = _dec(UpstreamState.diverged, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="finish", ignored=[]) == GateAction.block


def test_should_block_ignored_wins_over_settled_logic():
    """ignored list short-circuits everything including settled auto-allow."""
    d = _dec(UpstreamState.merged, finished_at="2026-04-18T13:20:28+00:00", slug="a")
    # Whether ignored or not, the answer is allow — but verify the ignored path fires first
    # by constructing a case where settled would block (doesn't exist in our logic,
    # but asserting the ignored-overrides-all invariant).
    assert should_block(d, command="finish", ignored=["a"]) == GateAction.allow
