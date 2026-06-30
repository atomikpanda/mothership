from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.message_store import MessageStore

runner = CliRunner()


def _bootstrap(tmp_path: Path) -> tuple[Path, Path, MessageStore]:
    """Workspace with a MessageStore; returns (cfg, state_dir, store)."""
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    store = MessageStore(state_dir / "messages")
    return cfg, state_dir, store


def _override(cfg, state_dir):
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override(); container.config.reset()
    container.state_manager.reset_override(); container.state_manager.reset()
    container.log_manager.reset()


def _event(stop_hook_active: bool = False) -> str:
    return json.dumps({"hook_event_name": "Stop", "stop_hook_active": stop_hook_active})


def test_blocks_when_threads_await(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    now = datetime.now(timezone.utc)
    store.create_thread("first idea", "shape this into a spec", now)
    store.create_thread("second", "and answer this", now)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_drain"], input=_event())
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["decision"] == "block"
        assert "shape this into a spec" in payload["reason"]
        assert "and answer this" in payload["reason"]
        assert "mship reply" in payload["reason"]
    finally:
        _reset()


def test_allows_when_inbox_empty(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_drain"], input=_event())
        assert result.exit_code == 0
        assert '"decision"' not in result.output
    finally:
        _reset()


def test_allows_when_thread_already_answered(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    now = datetime.now(timezone.utc)
    t = store.create_thread("s", "q", now)
    store.append(t.id, "agent", "answered", now)  # latest role == agent -> not awaiting
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_drain"], input=_event())
        assert result.exit_code == 0
        assert '"decision"' not in result.output
    finally:
        _reset()


def test_allows_when_stop_hook_active(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    store.create_thread("s", "still pending", datetime.now(timezone.utc))
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_drain"], input=_event(stop_hook_active=True))
        assert result.exit_code == 0
        assert '"decision"' not in result.output  # loop safety: never block twice
    finally:
        _reset()


def test_malformed_stdin_fails_open(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    store.create_thread("s", "pending", datetime.now(timezone.utc))
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_drain"], input="not json{{")
        assert result.exit_code == 0
        assert '"decision"' not in result.output
    finally:
        _reset()


def test_outside_workspace_fails_open(tmp_path: Path, monkeypatch):
    # No container override -> get_container(required=False) returns None.
    # chdir to tmp_path so ConfigLoader.discover() finds no mothership.yaml.
    monkeypatch.chdir(tmp_path)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    result = runner.invoke(app, ["_drain"], input=_event())
    assert result.exit_code == 0
    assert '"decision"' not in result.output
