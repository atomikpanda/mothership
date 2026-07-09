"""Tests for wiring PrWatcher into `mship serve` via an app lifespan
(src/mship/core/serve.py::_lifespan, _pr_watch_loop, PR_WATCH_INTERVAL_SECONDS).

Integration tests drive the real ASGI lifespan through `with TestClient(app):`
(startup + shutdown). They're kept fast/deterministic by seeding no tasks
with `pr_urls` — `PrWatcher.check_once()` then never calls `check_state`, so
no `gh` subprocess is invoked regardless of how many times the loop ticks.

`_pr_watch_loop` is also unit-tested directly (no FastAPI/TestClient) against
a fake watcher, per the task's suggestion to factor a small pure loop helper.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from mship.core.serve import PR_WATCH_INTERVAL_SECONDS, _pr_watch_loop, create_app
from mship.core.state import StateManager


def _app(tmp_path: Path):
    return create_app(
        specs_dir=tmp_path / "specs",
        state_manager=StateManager(tmp_path / ".mothership"),
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
    )


# --- lifespan integration (through TestClient) ---


def test_lifespan_starts_and_stops_cleanly_with_watcher_enabled(tmp_path, monkeypatch):
    # Tiny interval so the loop ticks at least once during the `with` block,
    # but no tasks with pr_urls exist, so check_once() is a cheap no-op sweep
    # (never reaches check_state, so no `gh` subprocess runs).
    monkeypatch.setenv("MSHIP_PR_WATCH_INTERVAL", "0.01")
    app = _app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
    # Exiting the `with` block ran ASGI shutdown (stop.set() + task.cancel())
    # without raising — that's the behavior under test.


def test_lifespan_interval_le_zero_disables_watcher(tmp_path, monkeypatch):
    monkeypatch.setenv("MSHIP_PR_WATCH_INTERVAL", "0")
    app = _app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200


def test_lifespan_negative_interval_disables_watcher(tmp_path, monkeypatch):
    monkeypatch.setenv("MSHIP_PR_WATCH_INTERVAL", "-5")
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_lifespan_default_interval_is_positive():
    # Sanity: the module default (no env override) enables the loop rather
    # than accidentally shipping disabled.
    assert PR_WATCH_INTERVAL_SECONDS > 0


# --- _pr_watch_loop unit tests (no FastAPI/TestClient, no real gh calls) ---


class _FakeWatcher:
    def __init__(self) -> None:
        self.calls = 0

    def check_once(self) -> None:
        self.calls += 1


async def test_pr_watch_loop_calls_check_once_and_exits_promptly_on_stop():
    watcher = _FakeWatcher()
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    stopper = asyncio.create_task(_stop_soon())
    # interval is large (10s); the loop must still exit promptly once `stop`
    # is set, rather than waiting out the full interval.
    await asyncio.wait_for(_pr_watch_loop(watcher, stop, interval=10), timeout=2)
    await stopper

    assert watcher.calls >= 1


async def test_pr_watch_loop_survives_check_once_exception():
    class _RaisingWatcher:
        def __init__(self) -> None:
            self.calls = 0

        def check_once(self) -> None:
            self.calls += 1
            raise RuntimeError("boom")

    watcher = _RaisingWatcher()
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    stopper = asyncio.create_task(_stop_soon())
    # Small interval so several raising sweeps happen before `stop` fires,
    # proving one exception doesn't kill the loop (it keeps ticking).
    await asyncio.wait_for(_pr_watch_loop(watcher, stop, interval=0.01), timeout=2)
    await stopper

    assert watcher.calls >= 2  # survived an exception and looped again
