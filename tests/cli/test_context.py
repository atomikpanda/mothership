"""Tests for the `mship context` CLI command (JSON wire format)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.cli.output import reset_output_settings
from mship.core.state import StateManager, Task, WorkspaceState


def _bootstrap(tmp_path: Path, slugs: list[str]) -> tuple[Path, Path]:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    tasks = {
        s: Task(
            slug=s, description=s, phase="dev",
            created_at=datetime.now(timezone.utc),
            affected_repos=["mothership"],
            branch=f"feat/{s}",
            base_branch="main",
        )
        for s in slugs
    }
    StateManager(state_dir).save(WorkspaceState(tasks=tasks))
    return cfg, state_dir


def _reset_container():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()


def test_context_emits_valid_json(tmp_path: Path):
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, ["alpha", "beta"])

    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["schema_version"] == "1"
        slugs = sorted(t["slug"] for t in data["active_tasks"])
        assert slugs == ["alpha", "beta"]
        for task in data["active_tasks"]:
            assert task["base_branch"] == "main"
            assert task["phase"] == "dev"
            assert task["finished_at"] is None
            assert task["drift"] == "unknown"  # no reconcile cache present
        assert data["main_checkout_clean"] == {}  # config has no repos
        assert data["last_workspace_fetch_at"] is None
        assert data["last_drift_check_at"] is None
    finally:
        _reset_container()


def test_context_empty_workspace(tmp_path: Path):
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])

    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["active_tasks"] == []
        assert data["cwd_matches_task"] is None
        assert data["cwd_matches_repo"] is None
    finally:
        _reset_container()


def test_context_includes_docs_dir(tmp_path: Path):
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])

    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["docs_dir"] == "docs"
    finally:
        _reset_container()


# --- MOS-100: --for/--kind audience-shaped output ---------------------------


def test_context_no_for_flag_omits_audience_key(tmp_path: Path):
    """ac1: no --for -> output identical to today's schema, no `audience` key."""
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "audience" not in data
    finally:
        _reset_container()


@pytest.mark.parametrize("for_value", ["claude-code", "codex"])
def test_context_for_implementer_emits_audience_block(tmp_path: Path, for_value: str):
    """ac2: --for claude-code / codex emit the base payload plus an audience
    block with the implementer framing."""
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context", "--for", for_value])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["schema_version"] == "1"  # base payload still present
        assert data["audience"]["for"] == for_value
        assert data["audience"]["kind"] is None
        assert "mship commit" in data["audience"]["instructions"]
    finally:
        _reset_container()


def test_context_for_human_emits_audience_block(tmp_path: Path):
    """ac3: --for human emits a prose-style human summary instruction."""
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context", "--for", "human"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["audience"]["for"] == "human"
        assert data["audience"]["kind"] is None
    finally:
        _reset_container()


def test_context_for_reviewer_kind_spec_emits_audience_block(tmp_path: Path):
    """ac4: --for reviewer --kind spec instructs verifying against the task
    description/plan and flagging over-/under-building."""
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context", "--for", "reviewer", "--kind", "spec"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["audience"] == {
            "for": "reviewer",
            "kind": "spec",
            "instructions": data["audience"]["instructions"],
        }
        assert "spec" in data["audience"]["instructions"].lower()
    finally:
        _reset_container()


def test_context_for_reviewer_kind_code_quality_emits_audience_block(tmp_path: Path):
    """ac5: --for reviewer --kind code-quality instructs inspecting the diff
    for maintainability, naming, test quality, and regressions."""
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context", "--for", "reviewer", "--kind", "code-quality"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["audience"]["for"] == "reviewer"
        assert data["audience"]["kind"] == "code-quality"
        assert "maintainability" in data["audience"]["instructions"].lower()
    finally:
        _reset_container()


def test_context_kind_without_for_reviewer_is_rejected(tmp_path: Path):
    """ac6: --kind without --for reviewer is a clean CLI error, not a crash."""
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context", "--kind", "spec"])
        assert result.exit_code != 0
        assert "reviewer" in result.output.lower()
    finally:
        _reset_container()


def test_context_kind_with_non_reviewer_for_is_rejected(tmp_path: Path):
    """ac6: --kind with a non-reviewer --for is rejected."""
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context", "--for", "human", "--kind", "spec"])
        assert result.exit_code != 0
        assert "reviewer" in result.output.lower()
    finally:
        _reset_container()


def test_context_reviewer_without_kind_is_rejected(tmp_path: Path):
    """ac7: --for reviewer without --kind is rejected (kind is required)."""
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context", "--for", "reviewer"])
        assert result.exit_code != 0
        assert "--kind" in result.output
    finally:
        _reset_container()


def test_context_unknown_for_value_is_rejected(tmp_path: Path):
    """Unknown --for values are rejected with a clean CLI error."""
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context", "--for", "gemini"])
        assert result.exit_code != 0
    finally:
        _reset_container()


def test_context_tty_renders_markdown_audience_block(tmp_path: Path, monkeypatch):
    """ac9 + MOS-177: in human mode the instructions render to STDERR as a
    readable block, while STDOUT stays a PURE JSON stream (a human preamble on
    stdout would break callers that parse `mship context` output as JSON). The
    audience is also carried in the JSON payload."""
    runner = CliRunner()  # click 8.2+: stdout/stderr are already separate
    cfg, state_dir = _bootstrap(tmp_path, [])
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    monkeypatch.setenv("MSHIP_JSON", "0")  # force human_mode without a real tty
    reset_output_settings()
    try:
        result = runner.invoke(app, ["context", "--for", "human"])
        assert result.exit_code == 0, result.output
        # STDOUT is pure, parseable JSON — no prose preamble polluting it.
        data = json.loads(result.stdout)
        assert data["audience"]["for"] == "human"
        # The human-readable block renders to STDERR (alongside breadcrumbs).
        assert "Audience" in result.stderr or "human" in result.stderr
    finally:
        reset_output_settings()
        _reset_container()
