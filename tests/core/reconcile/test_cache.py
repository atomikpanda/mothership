import json
import time
import fcntl
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
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


@pytest.mark.parametrize("operation", ["add", "remove", "clear", "invalidate", "write"])
def test_cache_mutations_serialize_with_concurrent_result_writer(tmp_path, monkeypatch, operation):
    first = ReconcileCache(tmp_path)
    second = ReconcileCache(tmp_path)
    first.write(CachePayload(fetched_at=10, ttl_seconds=300, results={"old": {}}, ignored=["a"]))
    paused = threading.Event()
    release = threading.Event()
    contender = threading.Event()
    first_ident = []
    second_ident = []
    original_read = Path.read_text
    original_write = Path.write_text
    original_flock = fcntl.flock

    def pause():
        paused.set()
        assert release.wait(5), "interleaving did not release first writer"

    def read(path, *args, **kwargs):
        value = original_read(path, *args, **kwargs)
        if threading.get_ident() in first_ident and operation != "write":
            pause()
        return value

    def write(path, *args, **kwargs):
        value = original_write(path, *args, **kwargs)
        if threading.get_ident() in first_ident and operation == "write":
            pause()
        return value

    def flock(fd, mode):
        if threading.get_ident() in second_ident and mode == fcntl.LOCK_EX:
            contender.set()
        return original_flock(fd, mode)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(fcntl, "flock", flock)

    def mutate():
        first_ident.append(threading.get_ident())
        try:
            if operation == "add":
                first.add_ignore("b")
            elif operation == "remove":
                first.remove_ignore("a")
            elif operation == "clear":
                first.clear_ignores()
            elif operation == "invalidate":
                first.invalidate()
            else:
                first.write(CachePayload(fetched_at=20, ttl_seconds=300, results={"first": {}}))
        finally:
            paused.set()

    def replace_results():
        second_ident.append(threading.get_ident())
        try:
            second.write(CachePayload(fetched_at=30, ttl_seconds=300, results={"new": {}}, ignored=["new-ignore"]))
        finally:
            # Without a lock the second write completes before resuming the
            # first; with a lock its acquisition attempt releases the barrier.
            contender.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(mutate)
        try:
            assert paused.wait(5)
            if one.done():
                one.result()
            two = pool.submit(replace_results)
            assert contender.wait(5)
        finally:
            release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    saved = first.read()
    assert saved.results == {"new": {}}
    assert saved.ignored == ["new-ignore"]
    assert saved.fetched_at == 30


def test_invalidate_preserves_payload_and_ignores(tmp_path):
    cache = ReconcileCache(tmp_path)
    cache.invalidate()
    assert cache.read() is None
    payload = CachePayload(fetched_at=50, ttl_seconds=123, results={"a": {}}, ignored=["keep"], schema_version=0, base_context={"a": ["release"]})
    cache.write(payload)
    cache.invalidate()
    saved = cache.read()
    assert saved.fetched_at == 0
    assert saved.results == {"a": {}}
    assert saved.ignored == ["keep"]
    assert saved.schema_version == 0
    assert saved.base_context == {"a": ["release"]}
    assert saved.ttl_seconds == 123


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


def test_current_scoped_context_ignores_malformed_unrequested_entries(tmp_path: Path):
    cache = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(
        fetched_at=time.time(),
        ttl_seconds=300,
        results={"selected": {"state": "in_sync"}, "unrelated": []},
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
