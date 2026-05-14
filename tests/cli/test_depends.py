"""Tests for the `mship depends` verb group."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


def _seed(workspace: Path, *tasks: Task) -> None:
    sm = StateManager(workspace / ".mothership")
    sm.save(WorkspaceState(tasks={t.slug: t for t in tasks}))


def _task(slug: str) -> Task:
    return Task(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"], branch=f"feat/{slug}",
    )


@pytest.fixture
def configured_app(workspace: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    (workspace / ".mothership").mkdir(exist_ok=True)
    yield
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_depends_add_creates_edge(workspace, configured_app):
    _seed(workspace, _task("a"), _task("b"))
    result = runner.invoke(app, ["depends", "add", "a", "--task", "b"])
    assert result.exit_code == 0, result.stderr or result.output

    sm = StateManager(workspace / ".mothership")
    state = sm.load()
    edges = state.tasks["b"].depends_on
    assert len(edges) == 1
    assert edges[0].upstream_slug == "a"


def test_depends_add_unknown_upstream_errors(workspace, configured_app):
    _seed(workspace, _task("b"))
    result = runner.invoke(app, ["depends", "add", "nope", "--task", "b"])
    assert result.exit_code != 0
    assert "nope" in (result.stderr or result.output).lower()


def test_depends_add_self_edge_rejected(workspace, configured_app):
    _seed(workspace, _task("b"))
    result = runner.invoke(app, ["depends", "add", "b", "--task", "b"])
    assert result.exit_code != 0
    err = (result.stderr or result.output).lower()
    assert "cycle" in err or "self" in err


def test_depends_add_cycle_rejected(workspace, configured_app):
    """b already depends on a; adding a→b creates a cycle."""
    a = _task("a")
    b = _task("b")
    from mship.core.state import DependencyEdge
    b.depends_on = [DependencyEdge(upstream_slug="a", created_at=datetime.now(timezone.utc))]
    _seed(workspace, a, b)

    result = runner.invoke(app, ["depends", "add", "b", "--task", "a"])
    assert result.exit_code != 0
    err = (result.stderr or result.output).lower()
    assert "cycle" in err
    assert "b" in err and "a" in err


def test_depends_add_duplicate_idempotent(workspace, configured_app):
    """Adding an existing edge is a no-op (does not duplicate)."""
    _seed(workspace, _task("a"), _task("b"))
    runner.invoke(app, ["depends", "add", "a", "--task", "b"])
    result = runner.invoke(app, ["depends", "add", "a", "--task", "b"])
    assert result.exit_code == 0
    sm = StateManager(workspace / ".mothership")
    edges = sm.load().tasks["b"].depends_on
    assert len(edges) == 1
