from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.message_store import MessageStore
from mship.core.state import StateManager


def _client(tmp_path: Path, auth_token=None) -> tuple[TestClient, MessageStore]:
    # Mirror the existing tests/core/test_serve.py::_app construction (create_app
    # requires specs_dir/state_manager/log_manager/workspace_root/workspace_name).
    app = create_app(
        specs_dir=tmp_path / "specs",
        state_manager=StateManager(tmp_path / ".mothership"),
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
        auth_token=auth_token,
    )
    store = MessageStore(tmp_path / ".mothership" / "messages")
    return TestClient(app), store


PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def test_plain_get_threads_unchanged(tmp_path: Path):
    client, store = _client(tmp_path)
    store.create_thread("s", "hi", datetime.now(timezone.utc))
    r = client.get("/threads")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and body[0]["subject"] == "s"  # list shape preserved


def test_wait_returns_changed_when_newer_than_since(tmp_path: Path):
    client, store = _client(tmp_path)
    store.create_thread("s", "hi", datetime.now(timezone.utc))
    r = client.get("/threads", params={"wait": 1, "since": PAST, "timeout": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["timed_out"] is False
    assert body["threads"][0]["subject"] == "s"
    assert "cursor" in body


def test_wait_times_out_with_empty_list(tmp_path: Path):
    client, _ = _client(tmp_path)
    r = client.get("/threads", params={"wait": 1, "timeout": 0.1})  # since defaults to now
    assert r.status_code == 200
    body = r.json()
    assert body["timed_out"] is True
    assert body["threads"] == []


def test_wait_requires_auth(tmp_path: Path):
    client, _ = _client(tmp_path, auth_token="secret")
    r = client.get("/threads", params={"wait": 1, "timeout": 0.1})
    assert r.status_code == 401


def test_wait_invalid_since_returns_422(tmp_path: Path):
    # A malformed ?since= must be a clean 422, not a 500 from an unhandled ValueError.
    client, _ = _client(tmp_path)
    r = client.get("/threads", params={"wait": 1, "since": "notadate", "timeout": 0.1})
    assert r.status_code == 422
