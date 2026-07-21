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
            return WorkspaceState(tasks={"a": _task("a"), "b": _task("b")})

    view = StatusView(state_manager=FakeSM(), workspace_root=tmp_path, task_filter=None)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
    assert "Task:   a" in text
    assert "Task:   b" in text


def test_status_view_gather_reflects_state_changes(tmp_path):
    """Ensure StatusView.gather() reads fresh state each call (post-close refresh)."""
    from mship.cli.view.status import StatusView
    from mship.core.state import StateManager, WorkspaceState, Task
    from datetime import datetime, timezone

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    sm = StateManager(state_dir)

    # Save: one active task.
    task = Task(
        slug="a", description="a", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], worktrees={}, branch="feat/a", base_branch="main",
    )
    sm.save(WorkspaceState(tasks={"a": task}))

    view = StatusView(state_manager=sm, workspace_root=tmp_path, task_filter=None)
    before = view.gather()
    assert "Task:   a" in before

    # Simulate close — remove task.
    sm.save(WorkspaceState(tasks={}))

    after = view.gather()
    assert "No tasks" in after
    assert "Task:   a" not in after


def test_status_cli_rejects_unknown_task(tmp_path: Path):
    runner = CliRunner()
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    StateManager(state_dir).save(WorkspaceState(tasks={}))

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


# --- PR1: WorkItem grouping (AC5) ---
from mship.core.workitem import WorkItem as _WorkItem
from mship.core.view.workitem_index import build_workitem_index as _bwi


def test_status_view_groups_tasks_under_workitem(tmp_path):
    from mship.cli.view.status import StatusView
    now = datetime.now(timezone.utc)
    tasks = {"a": _task("a", phase="dev"), "b": _task("b", phase="review"),
             "c": _task("c", phase="plan")}

    class SM:
        def load(self):
            return WorkspaceState(tasks=tasks)

    wi = _WorkItem(id="wi-1", title="Overhaul", workspace="ws", kind="feature",
                   created_at=now, updated_at=now, task_slugs=["a", "b"])
    workitems = _bwi([wi], {}, tasks, {})

    view = StatusView(state_manager=SM(), workspace_root=tmp_path, task_filter=None,
                      workitem_loader=lambda: workitems)
    text = view.gather()
    assert "wi-1" in text and "Overhaul" in text
    # Grouped tasks appear with their own phase lines.
    assert "Task:   a" in text and "Task:   b" in text
    assert "Phase:  dev" in text and "Phase:  review" in text
    # Unlinked task 'c' still shown (trailing ungrouped block).
    assert "Task:   c" in text


def test_status_view_without_loader_is_flat(tmp_path):
    from mship.cli.view.status import StatusView

    class SM:
        def load(self):
            return WorkspaceState(tasks={"a": _task("a"), "b": _task("b")})

    view = StatusView(state_manager=SM(), workspace_root=tmp_path, task_filter=None)
    text = view.gather()
    assert "Task:   a" in text and "Task:   b" in text
    assert "◆" not in text  # no WorkItem header without a loader


# --- cockpit-v2 Task 6: status --follow ---
from typer.testing import CliRunner as _CliRunner

from mship.cli import app as _app6, container as _c6
from mship.core.focus import focus_path, write_focus
from mship.core.spec import Spec as _Spec6
from mship.core.spec_store import SPECS_DIRNAME as _SPECS6, SpecStore as _SpecStore6
from mship.core.state import StateManager as _SM6, Task as _Task6, WorkspaceState as _WS6
from mship.core.workitem import WorkItem as _WI6
from mship.core.workitem_store import WorkItemStore as _WIS6


def _now_dt6():
    return datetime(2026, 7, 21, tzinfo=timezone.utc)


def _seed_follow(tmp_path, worktrees):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir(exist_ok=True)
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    _SpecStore6(tmp_path / _SPECS6).save(_Spec6(
        id="spec-1", title="T", status="approved",
        created_at=_now_dt6(), updated_at=_now_dt6(), body="b\n"))
    _WIS6(state_dir / "workitems").save(_WI6(
        id="wi-1", title="T", workspace="t", kind="feature",
        created_at=_now_dt6(), updated_at=_now_dt6(), spec_id="spec-1", task_slugs=["a"]))
    _SM6(state_dir).save(_WS6(tasks={"a": _Task6(
        slug="a", description="d", phase="dev", created_at=_now_dt6(),
        affected_repos=["r"], branch="feat/a", worktrees=worktrees, work_item_id="wi-1")}))
    _c6.config.reset(); _c6.state_manager.reset()
    _c6.config_path.override(tmp_path / "mothership.yaml")
    _c6.state_dir.override(state_dir)
    return state_dir


def _reset_follow():
    _c6.config_path.reset_override(); _c6.state_dir.reset_override()
    _c6.config.reset_override(); _c6.config.reset()
    _c6.state_manager.reset_override(); _c6.state_manager.reset()


def test_status_follow_no_focus_prints_hint(tmp_path):
    _seed_follow(tmp_path, {"r": tmp_path})
    try:
        result = _CliRunner().invoke(_app6, ["view", "status", "--follow"])
        assert result.exit_code == 0, result.output
        assert "no workitem focused" in result.output.lower()
    finally:
        _reset_follow()


def test_status_follow_scopes_to_focused_task(tmp_path):
    state_dir = _seed_follow(tmp_path, {"r": tmp_path})
    write_focus(focus_path(state_dir), "wi-1")
    try:
        result = _CliRunner().invoke(_app6, ["view", "status", "--follow"])
        assert result.exit_code == 0, result.output
        assert "feat/a" in result.output   # the focused task's branch
    finally:
        _reset_follow()
