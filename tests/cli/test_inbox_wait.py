from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.message_store import MessageStore

runner = CliRunner()


def _bootstrap(tmp_path: Path) -> tuple[Path, Path, MessageStore]:
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"; cfg.write_text("workspace: t\nrepos: {}\n")
    return cfg, state_dir, MessageStore(state_dir / "messages")


def _override(cfg, state_dir):
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg); container.state_dir.override(state_dir)


def _reset():
    container.config_path.reset_override(); container.state_dir.reset_override()
    container.config.reset_override(); container.config.reset()
    container.state_manager.reset_override(); container.state_manager.reset()
    container.log_manager.reset()


PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def test_wait_returns_awaiting_thread_immediately_when_newer_than_since(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    store.create_thread("idea", "shape this", datetime.now(timezone.utc))  # awaiting (human)
    _override(cfg, state_dir)
    try:
        # --since in the past => the existing thread counts as new => first poll hits.
        result = runner.invoke(app, ["inbox", "wait", "--since", PAST, "--timeout", "5"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["timed_out"] is False
        assert payload["threads"][0]["pending"] == "shape this"
        assert "cursor" in payload
    finally:
        _reset()


def test_wait_times_out_when_no_new_message(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["inbox", "wait", "--timeout", "0.1"])  # default since=now
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["timed_out"] is True
        assert payload["threads"] == []
    finally:
        _reset()


def test_wait_ignores_agent_reply(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    t = store.create_thread("s", "q", datetime.now(timezone.utc))
    store.append(t.id, "agent", "answered", datetime.now(timezone.utc))  # latest = agent
    _override(cfg, state_dir)
    try:
        # Even with --since in the past, an agent-latest thread is not awaiting => no hit.
        result = runner.invoke(app, ["inbox", "wait", "--since", PAST, "--timeout", "0.1"])
        payload = json.loads(result.output)
        assert payload["timed_out"] is True
        assert payload["threads"] == []
    finally:
        _reset()


def test_wait_outside_workspace_errors(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    container.config_path.reset_override(); container.state_dir.reset_override()
    container.config.reset_override(); container.config.reset()
    container.state_manager.reset_override(); container.state_manager.reset()
    try:
        result = runner.invoke(app, ["inbox", "wait", "--timeout", "0.1"])
        assert result.exit_code != 0  # required container -> clear error outside a workspace
    finally:
        _reset()


def test_wait_invalid_since_errors_cleanly(tmp_path: Path):
    # A malformed --since must produce a clean error, not an unhandled traceback.
    cfg, state_dir, store = _bootstrap(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["inbox", "wait", "--since", "notadate", "--timeout", "0.1"])
        assert result.exit_code == 2
        assert "invalid" in result.output.lower() and "since" in result.output.lower()
    finally:
        _reset()
