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
        base_context={"a": ["main"]},
    ))
    state = WorkspaceState(tasks={"a": _task("a")})
    decisions = reconcile_now(state, cache=cache, fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("should not fetch")))
    assert decisions["a"].state == UpstreamState.merged


def test_reconcile_now_applies_dependency_stale_from_fresh_cache(tmp_path: Path):
    # #104 regression: dependency_stale is derived from LIVE task state (depends_on edges + the
    # upstream's merge state), so it must apply on the cache-hit path too — not only the fresh fetch.
    # A warm cache (the common case: reconcile shares one 300s cache across spawn/finish/close/
    # precommit) must NOT silently downgrade a dependency-stale task to in_sync, which would let it
    # finish against a stale base.
    from mship.core.state import DependencyEdge
    t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 10, tzinfo=timezone.utc)
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={
            "a": {"state": "merged", "pr_url": None, "pr_number": None, "base": None,
                  "merge_commit": None, "updated_at": t1.isoformat()},
            "b": {"state": "in_sync"},
        },
        ignored=[],
        base_context={"a": ["main"], "b": ["main"]},
    ))
    state = WorkspaceState(tasks={
        "a": _task("a", created_at=t0, finished_at=t0),
        "b": _task("b", created_at=t0,
                   depends_on=[DependencyEdge(upstream_slug="a", created_at=t0)]),
    })
    # A scoped cache hit still needs the cached upstream to derive b's state,
    # while returning only the selected task.
    decisions = reconcile_now(
        state, cache=cache,
        fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("should not fetch")),
        only_slugs={"b"},
    )
    assert set(decisions) == {"b"}
    assert decisions["b"].state == UpstreamState.dependency_stale


def test_reconcile_now_resolves_base_via_repo_config_not_raw_recorded_base(tmp_path: Path):
    """#455 Part 1: task.base_branch records the spawn-time default ('main'),
    but `finish` opens the PR against the RESOLVED base (repo_config.base_branch
    when no --base override was pinned). If a repo's configured base is 'dev',
    a PR opened against 'dev' is NOT drift — reconcile must compare against the
    same resolved base finish uses, not the raw recorded task.base_branch.
    """
    from mship.core.reconcile.fetch import FetchError  # noqa: F401 (documents fetcher contract)

    class _Repo:
        base_branch = "dev"

    config = type("Config", (), {"repos": {"r": _Repo()}})()
    cache = ReconcileCache(tmp_path)
    state = WorkspaceState(tasks={"a": _task("a", base_branch="main")})

    def _fetcher(branches, wts):
        return (
            {"feat/a": PRSnapshot(head_ref="feat/a", state="OPEN", base_ref="dev",
                                   merge_commit=None, url="https://x/pr/1", updated_at="z")},
            {"feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=1)},
        )

    decisions = reconcile_now(state, cache=cache, fetcher=_fetcher, config=config)
    assert decisions["a"].state == UpstreamState.in_sync


def test_reconcile_now_resolves_base_per_repo_for_multi_repo_task(tmp_path: Path):
    """#461 (follow-up to #455): a multi-repo task's repos can each have a
    DIFFERENT configured base_branch. reconcile matches PRs by branch name
    only (it can't tell which repo a fetched PR snapshot belongs to), so it
    must accept a PR whose base matches ANY of the task's repos' resolved
    bases — not just one repo (previously: task.active_repo or
    affected_repos[0]) arbitrarily chosen for the whole task. Resolving
    against only one repo's base falsely flags base_changed for a PR that's
    actually correctly targeting a *different* affected repo's base.
    """
    class _RepoMain:
        base_branch = "main"

    class _RepoDev:
        base_branch = "dev"

    config = type("Config", (), {"repos": {"r1": _RepoMain(), "r2": _RepoDev()}})()
    cache = ReconcileCache(tmp_path)
    state = WorkspaceState(tasks={
        "a": _task("a", base_branch="main", affected_repos=["r1", "r2"],
                    worktrees={"r1": Path("/tmp/fake/a-r1"), "r2": Path("/tmp/fake/a-r2")}),
    })

    def _fetcher(branches, wts):
        # Only one PR snapshot surfaces per branch (reconcile has no way to
        # tell which repo it came from) — here it's r2's PR, open against r2's
        # configured base ("dev"), not r1's ("main").
        return (
            {"feat/a": PRSnapshot(head_ref="feat/a", state="OPEN", base_ref="dev",
                                   merge_commit=None, url="https://x/pr/2", updated_at="z")},
            {"feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=1)},
        )

    decisions = reconcile_now(state, cache=cache, fetcher=_fetcher, config=config)
    assert decisions["a"].state == UpstreamState.in_sync


def test_reconcile_now_refetches_after_resolved_base_config_changes(tmp_path: Path):
    """A TTL-fresh result is incompatible with a newly resolved task base."""
    class _Repo:
        def __init__(self, base_branch: str) -> None:
            self.base_branch = base_branch

    cache = ReconcileCache(tmp_path)
    state = WorkspaceState(tasks={"a": _task("a")})
    calls = 0

    def _fetcher(branches, wts):
        nonlocal calls
        calls += 1
        return (
            {"feat/a": PRSnapshot(head_ref="feat/a", state="OPEN", base_ref="release",
                                   merge_commit=None, url="https://x/pr/1", updated_at="z")},
            {"feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=1)},
        )

    initial_config = type("Config", (), {"repos": {"r": _Repo("main")}})()
    changed_config = type("Config", (), {"repos": {"r": _Repo("release")}})()

    first = reconcile_now(state, cache=cache, fetcher=_fetcher, config=initial_config)
    second = reconcile_now(state, cache=cache, fetcher=_fetcher, config=changed_config)

    assert first["a"].state == UpstreamState.base_changed
    assert second["a"].state == UpstreamState.in_sync
    assert calls == 2


def test_reconcile_now_recomputes_when_cache_schema_is_stale(tmp_path: Path):
    """#461 follow-up: a cache entry written under the OLD (pre-#461) single-repo
    base-resolution logic can carry a spurious base_changed. Such an entry is
    still within its 300s TTL when the #461 fix deploys, so a naive freshness
    check would serve the stale verdict and shadow the fix until the TTL lapses
    or a manual refresh. The cache payload must carry a schema version so a
    pre-fix entry is treated as a miss (recomputed under the new per-repo
    resolution) rather than served as-is.
    """
    class _RepoDev:
        base_branch = "dev"

    config = type("Config", (), {"repos": {"r": _RepoDev()}})()
    cache = ReconcileCache(tmp_path)
    # Hand-craft a fresh-by-TTL cache entry on disk as the OLD (pre-schema-version,
    # pre-#461) logic would have written it: no "schema_version" key at all, and a
    # spurious base_changed from comparing against the wrong base. `cache.write()`
    # always stamps the CURRENT schema_version, so it can't produce this shape —
    # write the raw file directly to simulate a cache from before this fix existed.
    import json
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "reconcile.cache.json").write_text(json.dumps({
        "fetched_at": time.time(),
        "ttl_seconds": 300,
        "results": {"a": {"state": "base_changed", "pr_url": "u", "pr_number": 1, "base": "dev"}},
        "ignored": [],
    }))
    state = WorkspaceState(tasks={"a": _task("a", base_branch="main")})

    def _fetcher(branches, wts):
        return (
            {"feat/a": PRSnapshot(head_ref="feat/a", state="OPEN", base_ref="dev",
                                   merge_commit=None, url="https://x/pr/1", updated_at="z")},
            {"feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=1)},
        )

    decisions = reconcile_now(state, cache=cache, fetcher=_fetcher, config=config)
    assert decisions["a"].state == UpstreamState.in_sync


def test_reconcile_now_uses_fresh_cache_with_current_schema_version(tmp_path: Path):
    """A cache entry written under the CURRENT schema version is still used as-is
    (caching must not be broken by the version check)."""
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"}},
        ignored=[],
        base_context={"a": ["main"]},
    ))
    state = WorkspaceState(tasks={"a": _task("a")})
    decisions = reconcile_now(
        state, cache=cache,
        fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
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
        base_context={"a": ["main"]},
    ))
    state = WorkspaceState(tasks={"a": _task("a")})

    def bad_fetcher(*_):
        from mship.core.reconcile.fetch import FetchError
        raise FetchError("offline")

    decisions = reconcile_now(state, cache=cache, fetcher=bad_fetcher)
    assert decisions["a"].state == UpstreamState.merged


def test_reconcile_now_fetch_error_fallback_does_not_serve_schema_stale_cache(tmp_path: Path):
    """#461 follow-up P1 (cache.py:82, "Invalid cache survives fallback"):
    the fetch-error fallback used to return `payload` straight from
    `cache.read()` with no schema check at all, so on a live-fetch failure
    after the #461 upgrade, a pre-v2 entry's spurious base_changed would be
    served and block `finish`. A pre-v2 (schema-invalid) entry must be
    dropped at the load boundary, so the fallback has nothing stale left to
    serve — it degrades to `{}`, same as no cache at all. Must FAIL before
    the fix (stale base_changed served) and PASS after.
    """
    import json
    cache = ReconcileCache(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "reconcile.cache.json").write_text(json.dumps({
        "fetched_at": time.time(),
        "ttl_seconds": 300,
        "results": {"a": {"state": "base_changed", "pr_url": "u", "pr_number": 1, "base": "dev"}},
        "ignored": [],
        # no "schema_version" key — pre-v2 entry, TTL-fresh but schema-stale
    }))
    state = WorkspaceState(tasks={"a": _task("a")})

    def bad_fetcher(*_):
        from mship.core.reconcile.fetch import FetchError
        raise FetchError("offline")

    decisions = reconcile_now(state, cache=cache, fetcher=bad_fetcher)
    assert decisions == {}


def test_reconcile_now_returns_unavailable_on_error_without_cache(tmp_path: Path):
    cache = ReconcileCache(tmp_path)
    state = WorkspaceState(tasks={"a": _task("a")})
    def bad_fetcher(*_):
        from mship.core.reconcile.fetch import FetchError
        raise FetchError("offline")
    decisions = reconcile_now(state, cache=cache, fetcher=bad_fetcher)
    assert decisions == {}


def test_reconcile_now_scoped_fetch_error_uses_selected_compatible_cache(
    tmp_path: Path,
):
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time() - 9999,
        ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"}},
        ignored=[],
        base_context={"a": ["main"], "b": ["old-base"]},
    ))
    state = WorkspaceState(tasks={"a": _task("a"), "b": _task("b", base_branch="new-base")})

    def bad_fetcher(*_):
        from mship.core.reconcile.fetch import FetchError
        raise FetchError("offline")

    decisions = reconcile_now(
        state,
        cache=cache,
        fetcher=bad_fetcher,
        only_slugs={"a"},
    )

    assert decisions["a"].state == UpstreamState.merged


def test_reconcile_now_scoped_fetches_only_selected_task_snapshots(tmp_path: Path):
    cache = ReconcileCache(tmp_path)
    state = WorkspaceState(tasks={"a": _task("a"), "b": _task("b")})
    calls: list[tuple[list[str], dict[str, Path]]] = []

    def fetcher(branches, worktrees):
        calls.append((list(branches), dict(worktrees)))
        return (
            {"feat/a": PRSnapshot(head_ref="feat/a", state="OPEN", base_ref="main",
                                  merge_commit=None, url="u", updated_at="z")},
            {"feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=0)},
        )

    decisions = reconcile_now(
        state,
        cache=cache,
        fetcher=fetcher,
        only_slugs={"a"},
    )

    assert calls == [(["feat/a"], {"feat/a": Path("/tmp/fake/a")})]
    assert set(decisions) == {"a"}


def test_reconcile_now_scoped_context_miss_fetches_transitive_dependencies(
    tmp_path: Path,
):
    """A selected context miss must still retain dependency-stale detection."""
    from mship.core.state import DependencyEdge

    created = datetime(2026, 5, 1, tzinfo=timezone.utc)
    merged = datetime(2026, 5, 10, tzinfo=timezone.utc)
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time(),
        ttl_seconds=300,
        results={
            "root": {"state": "in_sync"},
            "a": {"state": "merged", "updated_at": merged.isoformat()},
            "b": {"state": "in_sync"},
            "unrelated": {"state": "merged"},
        },
        ignored=[],
        base_context={
            "root": ["main"],
            "a": ["main"],
            "b": ["old-base"],
            "unrelated": ["main"],
        },
    ))
    state = WorkspaceState(tasks={
        "root": _task("root", created_at=created),
        "a": _task(
            "a",
            created_at=created,
            finished_at=created,
            depends_on=[
                DependencyEdge(upstream_slug="root", created_at=created),
            ],
        ),
        "b": _task(
            "b",
            created_at=created,
            base_branch="new-base",
            depends_on=[
                DependencyEdge(upstream_slug="a", created_at=created),
            ],
        ),
        "unrelated": _task("unrelated"),
    })
    calls: list[list[str]] = []

    def fetcher(branches, worktrees):
        calls.append(list(branches))
        return (
            {
                "feat/root": PRSnapshot(
                    head_ref="feat/root",
                    state="OPEN",
                    base_ref="main",
                    merge_commit=None,
                    url="https://x/pr/root",
                    updated_at=created.isoformat(),
                ),
                "feat/a": PRSnapshot(
                    head_ref="feat/a",
                    state="MERGED",
                    base_ref="main",
                    merge_commit="a-merge",
                    url="https://x/pr/a",
                    updated_at=merged.isoformat(),
                ),
                "feat/b": PRSnapshot(
                    head_ref="feat/b",
                    state="OPEN",
                    base_ref="new-base",
                    merge_commit=None,
                    url="https://x/pr/b",
                    updated_at=created.isoformat(),
                ),
            },
            {
                "feat/root": GitSnapshot(
                    has_upstream=True,
                    behind=0,
                    ahead=0,
                ),
                "feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=0),
                "feat/b": GitSnapshot(has_upstream=True, behind=0, ahead=0),
            },
        )

    decisions = reconcile_now(
        state,
        cache=cache,
        fetcher=fetcher,
        only_slugs={"b"},
    )

    assert calls == [["feat/root", "feat/a", "feat/b"]]
    assert set(decisions) == {"b"}
    assert decisions["b"].state == UpstreamState.dependency_stale


def test_reconcile_now_scoped_fetch_does_not_refresh_unrelated_cache(
    tmp_path: Path,
):
    cache = ReconcileCache(tmp_path)
    original = CachePayload(
        fetched_at=time.time() - 9999,
        ttl_seconds=300,
        results={
            "a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"},
            "b": {"state": "merged", "pr_url": "u", "pr_number": 2, "base": "main"},
        },
        ignored=["b"],
        base_context={"a": ["main"], "b": ["main"]},
    )
    cache.write(original)
    state = WorkspaceState(tasks={"a": _task("a"), "b": _task("b")})

    decisions = reconcile_now(
        state,
        cache=cache,
        fetcher=lambda *_: (
            {"feat/a": PRSnapshot(head_ref="feat/a", state="OPEN", base_ref="main",
                                  merge_commit=None, url="u", updated_at="z")},
            {"feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=0)},
        ),
        only_slugs={"a"},
    )

    cached = cache.read()
    assert decisions["a"].state == UpstreamState.in_sync
    assert cached is not None
    assert cached.fetched_at == original.fetched_at
    assert cached.results == original.results


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
        base_context={"a": ["main"]},
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
