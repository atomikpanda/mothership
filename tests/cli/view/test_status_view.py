import pytest
from pathlib import Path

from mship.cli.view.status import StatusView


class _FakeTask:
    slug = "t1"
    phase = "impl"
    blocked_reason = None
    blocked_at = None
    branch = "feat/x"
    affected_repos = ["repo-a", "repo-b"]
    worktrees = {"repo-a": "/tmp/wta", "repo-b": "/tmp/wtb"}
    test_results = {}
    finished_at = None
    phase_entered_at = None


class _FakeState:
    current_task = "t1"
    tasks = {"t1": _FakeTask()}


class _FakeStateManager:
    def load(self):
        return _FakeState()


@pytest.mark.asyncio
async def test_status_view_renders_task(tmp_path: Path):
    view = StatusView(
        state_manager=_FakeStateManager(),
        workspace_root=tmp_path,
        task_filter="t1",
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "t1" in text
        assert "impl" in text
        assert "repo-a" in text


@pytest.mark.asyncio
async def test_status_view_no_active_task(tmp_path: Path):
    class _Empty:
        current_task = None
        tasks = {}

    class _Mgr:
        def load(self):
            return _Empty()

    view = StatusView(
        state_manager=_Mgr(),
        workspace_root=tmp_path,
        task_filter=None,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No tasks" in view.rendered_text()


@pytest.mark.asyncio
async def test_status_view_shows_finished_warning(tmp_path: Path):
    from datetime import datetime, timezone, timedelta

    class _Task:
        slug = "t"
        phase = "review"
        phase_entered_at = datetime.now(timezone.utc) - timedelta(hours=1)
        blocked_reason = None
        blocked_at = None
        branch = "feat/t"
        affected_repos = ["r"]
        worktrees = {}
        test_results = {}
        pr_urls = {}
        finished_at = datetime.now(timezone.utc) - timedelta(hours=2)

    class _State:
        current_task = "t"
        tasks = {"t": _Task()}

    class _Mgr:
        def load(self):
            return _State()

    view = StatusView(
        state_manager=_Mgr(),
        workspace_root=tmp_path,
        task_filter="t",
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "Finished" in text or "finished" in text
        assert "mship close" in text


@pytest.mark.asyncio
async def test_status_view_shows_active_repo(tmp_path: Path):
    from datetime import datetime, timezone

    class _Task:
        slug = "t"
        phase = "dev"
        phase_entered_at = datetime.now(timezone.utc)
        blocked_reason = None
        blocked_at = None
        branch = "feat/t"
        affected_repos = ["a", "b"]
        worktrees = {}
        test_results = {}
        pr_urls = {}
        finished_at = None
        active_repo = "a"

    class _State:
        current_task = "t"
        tasks = {"t": _Task()}

    class _Mgr:
        def load(self):
            return _State()

    view = StatusView(
        state_manager=_Mgr(),
        workspace_root=tmp_path,
        task_filter="t",
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "Active repo" in text
        assert "a" in text


# --- Task 4 additions ---

from datetime import datetime, timezone

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


def _task(slug: str, **over) -> Task:
    base = dict(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"], worktrees={}, branch=f"feat/{slug}",
    )
    base.update(over)
    return Task(**base)


@pytest.mark.asyncio
async def test_status_view_stacks_all_tasks(monkeypatch, tmp_path: Path):
    from mship.cli.view.status import StatusView

    class FakeSM:
        def load(self):
            return WorkspaceState(tasks={"a": _task("a"), "b": _task("b")}, current_task=None)

    view = StatusView(state_manager=FakeSM(), workspace_root=tmp_path, task_filter=None)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
    assert "Task:   a" in text
    assert "Task:   b" in text


def test_status_cli_rejects_unknown_task(tmp_path: Path):
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
        result = runner.invoke(app, ["view", "status", "--task", "does-not-exist"])
        assert result.exit_code != 0
        assert "does-not-exist" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
        container.state_manager.reset_override()
        container.state_manager.reset()
