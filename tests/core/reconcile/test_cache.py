import json
import time
from pathlib import Path

from mship.core.reconcile.cache import ReconcileCache, CachePayload


def test_read_returns_none_when_file_absent(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    assert c.read() is None


def test_write_then_read_roundtrips(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(
        fetched_at=time.time(),
        ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "https://x/pr/1"}},
        ignored=[],
    )
    c.write(payload)
    got = c.read()
    assert got is not None
    assert got.results == {"a": {"state": "merged", "pr_url": "https://x/pr/1"}}
    assert got.ttl_seconds == 300


def test_is_fresh_true_within_ttl(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(fetched_at=time.time(), ttl_seconds=300, results={}, ignored=[])
    assert c.is_fresh(payload) is True


def test_is_fresh_false_after_ttl(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(fetched_at=time.time() - 1000, ttl_seconds=300, results={}, ignored=[])
    assert c.is_fresh(payload) is False


def test_add_ignore_persists(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    c.add_ignore("slug-a")
    assert "slug-a" in c.read_ignores()


def test_add_ignore_dedupes(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    c.add_ignore("slug-a")
    c.add_ignore("slug-a")
    assert c.read_ignores() == ["slug-a"]


def test_remove_ignore(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    c.add_ignore("slug-a")
    c.add_ignore("slug-b")
    c.remove_ignore("slug-a")
    assert c.read_ignores() == ["slug-b"]


def test_clear_ignores(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    c.add_ignore("slug-a")
    c.add_ignore("slug-b")
    c.clear_ignores()
    assert c.read_ignores() == []


def test_corrupt_cache_returns_none(tmp_path: Path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    (state_dir / "reconcile.cache.json").write_text("not json")
    c = ReconcileCache(state_dir)
    assert c.read() is None


def test_add_ignore_does_not_launder_a_pre_v2_entrys_schema_version(tmp_path: Path):
    # A TTL-fresh entry written before schema_version existed (spurious
    # base_changed baked in by the pre-#461 logic). It must stay stale after
    # an ignore mutation's read-modify-write, not get promoted to the current
    # schema_version — else the next reconcile would serve the stale result
    # (#461 follow-up).
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cache_path = state_dir / "reconcile.cache.json"
    cache_path.write_text(json.dumps({
        "fetched_at": time.time(),
        "ttl_seconds": 300,
        "results": {"a": {"state": "base_changed"}},
        "ignored": [],
        # no "schema_version" key — pre-v2 entry
    }))

    c = ReconcileCache(state_dir)
    c.add_ignore("a")

    payload = c.read()
    assert payload is not None
    assert payload.results == {"a": {"state": "base_changed"}}
    assert c.is_fresh(payload) is False


def test_clear_ignores_does_not_launder_a_pre_v2_entrys_schema_version(tmp_path: Path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cache_path = state_dir / "reconcile.cache.json"
    cache_path.write_text(json.dumps({
        "fetched_at": time.time(),
        "ttl_seconds": 300,
        "results": {"a": {"state": "base_changed"}},
        "ignored": ["a"],
        # no "schema_version" key — pre-v2 entry
    }))

    c = ReconcileCache(state_dir)
    c.clear_ignores()

    payload = c.read()
    assert payload is not None
    assert c.is_fresh(payload) is False


def test_write_stamps_current_schema_version_for_freshly_computed_results(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(
        fetched_at=time.time(),
        ttl_seconds=300,
        results={"a": {"state": "in_sync"}},
        ignored=[],
    )
    c.write(payload)
    got = c.read()
    assert got is not None
    assert c.is_fresh(got) is True


def test_current_scoped_context_ignores_unrequested_entries(tmp_path: Path):
    cache = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(
        fetched_at=time.time(),
        ttl_seconds=300,
        results={"selected": {"state": "in_sync"}, "unrelated": {"state": "merged"}},
        ignored=[],
        base_context={"selected": ["main"], "unrelated": ["old-base"]},
    )

    assert cache.current(
        payload,
        base_context={"selected": ["main"], "unrelated": ["new-base"]},
        only_slugs={"selected"},
    ) is payload


def test_current_scoped_context_requires_each_requested_entry(tmp_path: Path):
    cache = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(
        fetched_at=time.time(),
        ttl_seconds=300,
        results={"selected": {"state": "in_sync"}},
        ignored=[],
        base_context={"selected": ["main"]},
    )

    assert cache.current(
        payload,
        base_context={"selected": ["main"], "missing": ["main"]},
        only_slugs={"missing"},
    ) is None
    assert cache.current(
        payload,
        base_context={"selected": ["release"]},
        only_slugs={"selected"},
    ) is None


def test_current_scoped_context_requires_results_for_every_requested_entry(tmp_path: Path):
    cache = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(
        fetched_at=time.time(),
        ttl_seconds=300,
        results={"selected": {"state": "in_sync"}},
        ignored=[],
        base_context={"selected": ["main"], "dependency": ["main"]},
    )

    assert cache.current(
        payload,
        base_context={"selected": ["main"], "dependency": ["main"]},
        only_slugs={"selected", "dependency"},
    ) is None
