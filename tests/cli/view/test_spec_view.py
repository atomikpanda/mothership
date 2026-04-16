import threading
import urllib.request
from pathlib import Path

import pytest

from mship.cli.view.spec import SpecView, serve_spec_web


@pytest.mark.asyncio
async def test_spec_view_renders_markdown(tmp_path: Path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "s.md").write_text("# Hello\n\nBody text.\n")
    view = SpecView(workspace_root=tmp_path, name_or_path=None, watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        # SpecView uses Markdown widget; body text should appear
        assert "Body text" in view.rendered_text()


@pytest.mark.asyncio
async def test_spec_view_missing_spec(tmp_path: Path):
    view = SpecView(workspace_root=tmp_path, name_or_path="nope", watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "Spec not found" in view.rendered_text()


def test_serve_spec_web_serves_rendered_html(tmp_path: Path):
    spec = tmp_path / "s.md"
    spec.write_text("# Title\n\nBody.\n")
    server, port, thread = serve_spec_web(spec, start_port=47500)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            html = r.read().decode("utf-8")
        assert "<h1>" in html.lower() or "title" in html.lower()
        assert "body" in html.lower()
    finally:
        server.shutdown()
        thread.join(timeout=2)


# --- Task 5 additions ---

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, WorkspaceState


def test_spec_cli_rejects_task_with_name():
    runner = CliRunner()
    result = runner.invoke(app, ["view", "spec", "--task", "a", "some-name"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_spec_cli_rejects_unknown_task(tmp_path, monkeypatch):
    runner = CliRunner()
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    StateManager(state_dir).save(WorkspaceState(tasks={}, current_task=None))

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["view", "spec", "--task", "nope"])
        assert result.exit_code != 0
        assert "nope" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
        container.state_manager.reset_override()
        container.state_manager.reset()


# --- Spec fallback tests ---
from datetime import datetime, timezone
from mship.core.log import LogEntry
from mship.core.state import Task


class _StubLogManager:
    def __init__(self, entries: list[LogEntry]):
        self._entries = entries

    def read(self, slug: str, last=None):
        if last is None:
            return list(self._entries)
        return list(self._entries)[-last:]


def _stub_state_with_task(slug: str, description: str, phase: str, branch: str):
    """Build a minimal WorkspaceState-shaped stub with one task."""
    task = Task(
        slug=slug,
        description=description,
        phase=phase,
        branch=branch,
        created_at=datetime(2026, 4, 16, 0, 0, 0, tzinfo=timezone.utc),
        affected_repos=[],
        worktrees={},
        base_branch="main",
        active_repo=None,
    )
    state = WorkspaceState(tasks={slug: task}, current_task=slug)
    return state


@pytest.mark.asyncio
async def test_spec_fallback_renders_task_description_when_no_spec(tmp_path: Path):
    state = _stub_state_with_task(
        slug="demo-task",
        description="Build the demo feature end-to-end.",
        phase="dev",
        branch="feat/demo",
    )
    entries = [
        LogEntry(
            timestamp=datetime(2026, 4, 16, 3, 1, 49, tzinfo=timezone.utc),
            message="Task spawned",
        ),
        LogEntry(
            timestamp=datetime(2026, 4, 16, 3, 1, 58, tzinfo=timezone.utc),
            message="Phase transition: plan -> dev",
        ),
    ]
    log_manager = _StubLogManager(entries)

    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        task="demo-task",
        state=state,
        log_manager=log_manager,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        rendered = view.rendered_text()
        assert "Spec not found" not in rendered
        assert "demo-task" in rendered
        assert "Build the demo feature end-to-end." in rendered
        assert "Phase transition: plan -> dev" in rendered


@pytest.mark.asyncio
async def test_spec_explicit_name_still_errors_on_miss(tmp_path: Path):
    """Fallback only triggers when no name was specified; an explicit miss still errors."""
    state = _stub_state_with_task(
        slug="demo-task",
        description="desc",
        phase="dev",
        branch="feat/demo",
    )
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path="missing-spec",
        task="demo-task",
        state=state,
        log_manager=_StubLogManager([]),
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        rendered = view.rendered_text()
        assert "Spec not found" in rendered


@pytest.mark.asyncio
async def test_spec_fallback_handles_missing_log_manager(tmp_path: Path):
    """If no log_manager is wired, fallback still renders task description."""
    state = _stub_state_with_task(
        slug="demo-task",
        description="desc only",
        phase="plan",
        branch="feat/demo",
    )
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        task="demo-task",
        state=state,
        log_manager=None,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        rendered = view.rendered_text()
        assert "desc only" in rendered
        assert "No journal entries yet" in rendered


@pytest.mark.asyncio
async def test_spec_fallback_falls_back_to_error_when_no_task_resolvable(tmp_path: Path):
    """With no name, no task filter, and no current_task, show the original error."""
    empty_state = WorkspaceState(tasks={}, current_task=None)
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        task=None,
        state=empty_state,
        log_manager=None,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        rendered = view.rendered_text()
        assert "No specs found" in rendered or "Spec not found" in rendered
