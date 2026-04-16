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
