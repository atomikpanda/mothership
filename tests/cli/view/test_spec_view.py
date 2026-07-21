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


@pytest.mark.asyncio
async def test_spec_follow_survives_spec_file_vanishing(tmp_path: Path):
    # A --follow spec path can be deleted/renamed/replaced between the provider
    # resolving it and the read (spec transitions rewrite the file). The pane must
    # show the follow hint, not raise out of the timer callback (which would stop
    # the pane from ever refreshing again).
    from mship.cli.view._follow import follow_hint

    missing = tmp_path / "gone-spec.md"  # provider hands back a path that isn't there
    view = SpecView(workspace_root=tmp_path, name_or_path=None, watch=False,
                    interval=1.0, path_provider=lambda: missing)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert view._last_source == follow_hint()


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
    StateManager(state_dir).save(WorkspaceState(tasks={}))

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
    state = WorkspaceState(tasks={slug: task})
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
    """With no name, no task filter, and no task in state, show the original error."""
    empty_state = WorkspaceState(tasks={})
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


# --- watch-mode resolver tolerance ---

from dataclasses import dataclass as _dataclass, field as _field


@_dataclass
class _FakeSpecTask:
    slug: str
    description: str = ""
    phase: str = "plan"
    branch: str = "feat/x"
    worktrees: dict = _field(default_factory=dict)


class _FakeStateTasks:
    def __init__(self, tasks_dict):
        self.tasks = tasks_dict


class _MutableSpecStateMgr:
    def __init__(self, tasks_dict=None):
        self._tasks = tasks_dict or {}

    def set_tasks(self, tasks_dict):
        self._tasks = tasks_dict

    def load(self):
        return _FakeStateTasks(self._tasks)


@pytest.mark.asyncio
async def test_spec_view_watch_no_active_task_shows_placeholder(tmp_path):
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        state_manager=_MutableSpecStateMgr(tasks_dict={}),
        cli_task=None,
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No active task" in view.rendered_text()


@pytest.mark.asyncio
async def test_spec_view_watch_ambiguous_shows_placeholder(tmp_path):
    mgr = _MutableSpecStateMgr(tasks_dict={
        "alpha": _FakeSpecTask("alpha"),
        "beta":  _FakeSpecTask("beta"),
    })
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        state_manager=mgr,
        cli_task=None,
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "Multiple active tasks" in text
        assert "alpha" in text and "beta" in text


@pytest.mark.asyncio
async def test_spec_view_watch_unknown_slug_shows_placeholder(tmp_path):
    mgr = _MutableSpecStateMgr(tasks_dict={"other": _FakeSpecTask("other")})
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        state_manager=mgr,
        cli_task="missing-one",
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "missing-one" in view.rendered_text()


@pytest.mark.asyncio
async def test_spec_view_watch_transitions_to_fallback_when_task_appears(tmp_path):
    mgr = _MutableSpecStateMgr(tasks_dict={})
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        state_manager=mgr,
        cli_task=None,
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No active task" in view.rendered_text()
        mgr.set_tasks({
            "solo": _FakeSpecTask(
                "solo",
                description="My task description.",
                phase="plan",
                branch="feat/solo",
            ),
        })
        view._refresh_content()
        await pilot.pause()
        text = view.rendered_text()
        assert "No active task" not in text
        # Either a spec was rendered (none in tmp_path/docs/superpowers/specs)
        # or the task fallback with the description appears.
        assert "My task description" in text or "No spec yet for task" in text


# --- Non-watch TUI: no-active-task falls back to newest spec (MOS-175) ---


@pytest.mark.asyncio
async def test_spec_view_non_watch_no_active_task_renders_newest_spec(tmp_path):
    """A plain (non-watch) `mship view spec` with no active task must fall back
    to the newest spec — the watch-only 'No active task' placeholder is wrong
    here. Regression for the interactive TUI path that bypassed MOS-175."""
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "newest.md").write_text("# Newest Spec\n\nStructured spec body here.\n")
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        state_manager=_MutableSpecStateMgr(tasks_dict={}),
        cli_task=None,
        cwd=tmp_path,
        watch=False,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "No active task" not in text
        assert "Structured spec body here." in text


# --- Non-TTY short-circuit tests (#124) ---


def _setup_workspace(tmp_path: Path):
    """Configure container overrides for an empty workspace. Caller is
    responsible for resetting overrides via container.*.reset_override()."""
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    StateManager(state_dir).save(WorkspaceState(tasks={}))
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)


def _reset_overrides():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_spec_cli_non_tty_prints_spec_and_exits(tmp_path):
    """Non-TTY: short-circuit the TUI, print spec contents, exit 0. See #124."""
    runner = CliRunner()
    _setup_workspace(tmp_path)
    spec_file = tmp_path / "design.md"
    spec_file.write_text("# Hello\n\nSpec body content.\n")
    try:
        # Pass an explicit absolute path so task resolution is bypassed
        # (the bug is the TUI hang in non-TTY, not task lookup).
        result = runner.invoke(app, ["view", "spec", str(spec_file)])
        assert result.exit_code == 0, result.output
        assert "Spec body content." in result.output
    finally:
        _reset_overrides()


def test_spec_cli_non_tty_missing_spec_exits_1(tmp_path):
    """Non-TTY: missing spec errors with code 1, no TUI launch. See #124."""
    runner = CliRunner()
    _setup_workspace(tmp_path)
    try:
        result = runner.invoke(app, ["view", "spec", str(tmp_path / "nope.md")])
        assert result.exit_code == 1, result.output
        # Should be the SpecNotFoundError surfaced, not a TUI hang.
        assert "not found" in result.output.lower() or "no spec" in result.output.lower()
    finally:
        _reset_overrides()


def test_spec_cli_non_tty_does_not_construct_specview(tmp_path, monkeypatch):
    """Non-TTY path must not instantiate the Textual app at all. See #124."""
    runner = CliRunner()
    _setup_workspace(tmp_path)
    spec_file = tmp_path / "s.md"
    spec_file.write_text("# stub\nbody\n")

    constructed = []
    from mship.cli.view import spec as spec_mod
    real_init = spec_mod.SpecView.__init__

    def _trap_init(self, *a, **kw):
        constructed.append(True)
        real_init(self, *a, **kw)

    monkeypatch.setattr(spec_mod.SpecView, "__init__", _trap_init)
    try:
        result = runner.invoke(app, ["view", "spec", str(spec_file)])
        assert result.exit_code == 0, result.output
        assert constructed == [], "SpecView must not be constructed in non-TTY mode"
    finally:
        _reset_overrides()


# --- MOS-175: no-active-task fallback to newest structured spec ---


def test_spec_cli_non_tty_no_active_task_renders_newest_structured_spec(tmp_path):
    """Non-TTY, no active task: fall back to newest spec in <workspace>/specs/. MOS-175."""
    runner = CliRunner()
    _setup_workspace(tmp_path)
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "my-spec.md").write_text("# My Spec\n\nStructured spec body.\n")
    try:
        result = runner.invoke(app, ["view", "spec"])
        assert result.exit_code == 0, result.output
        assert "Structured spec body" in result.output
    finally:
        _reset_overrides()


def test_spec_cli_explicit_task_still_exits_on_unknown(tmp_path):
    """--task <unknown> still errors even with MOS-175 fallback in place."""
    runner = CliRunner()
    _setup_workspace(tmp_path)
    try:
        result = runner.invoke(app, ["view", "spec", "--task", "nope"])
        assert result.exit_code != 0
        assert "nope" in result.output
    finally:
        _reset_overrides()


# --- PR1: canonical selection (AC1, AC2) + spec header (AC9) ---
from mship.core.spec import Spec as _Spec
from mship.core.spec_store import SPECS_DIRNAME as _SPECS_DIRNAME, SpecStore as _SpecStore
from mship.core.state import Task as _Task
from mship.core.workitem import WorkItem as _WorkItem
from mship.core.workitem_store import WorkItemStore as _WorkItemStore


def _seed_canonical(tmp_path, spec_id, *, status="draft", day=1, body="", wi_id=None):
    now = datetime(2026, 7, day, tzinfo=timezone.utc)
    _SpecStore(tmp_path / _SPECS_DIRNAME).save(_Spec(
        id=spec_id, title=spec_id, status=status,
        created_at=now, updated_at=now, body=body))
    if wi_id is not None:
        _WorkItemStore(tmp_path / ".mothership" / "workitems").save(_WorkItem(
            id=wi_id, title=spec_id, workspace="t", kind="feature",
            created_at=now, updated_at=now, spec_id=spec_id))


def test_spec_cli_selects_by_workitem(tmp_path):
    runner = CliRunner()
    _setup_workspace(tmp_path)
    _seed_canonical(tmp_path, "spec-wi", body="Canonical body for wi-7\n", wi_id="wi-7")
    _seed_canonical(tmp_path, "spec-other", day=9, body="Other body\n")
    try:
        result = runner.invoke(app, ["view", "spec", "--workitem", "wi-7"])
        assert result.exit_code == 0, result.output
        assert "Canonical body for wi-7" in result.output
        assert "Other body" not in result.output
    finally:
        _reset_overrides()


def test_spec_cli_selects_by_status(tmp_path):
    runner = CliRunner()
    _setup_workspace(tmp_path)
    _seed_canonical(tmp_path, "spec-review", status="needs_review", body="Review me\n")
    _seed_canonical(tmp_path, "spec-approved", status="approved", day=9, body="Approved\n")
    try:
        result = runner.invoke(app, ["view", "spec", "--status", "needs_review"])
        assert result.exit_code == 0, result.output
        assert "Review me" in result.output
        assert "Approved" not in result.output
    finally:
        _reset_overrides()


def test_spec_cli_default_is_newest_by_created_at(tmp_path):
    runner = CliRunner()
    _setup_workspace(tmp_path)
    _seed_canonical(tmp_path, "spec-old", day=1, body="Old body\n")
    _seed_canonical(tmp_path, "spec-new", day=9, body="Newest body\n")
    try:
        result = runner.invoke(app, ["view", "spec"])
        assert result.exit_code == 0, result.output
        assert "Newest body" in result.output
    finally:
        _reset_overrides()


def test_spec_cli_default_ignores_task_worktree(tmp_path):
    """AC1: a spec that lives only in the canonical <root>/specs store renders
    even though the (only) task's worktree has no specs dir."""
    runner = CliRunner()
    _setup_workspace(tmp_path)
    wt = tmp_path / "wt-feature"
    wt.mkdir()
    StateManager(tmp_path / ".mothership").save(WorkspaceState(tasks={"a": _Task(
        slug="a", description="d", phase="dev",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        affected_repos=["r"], branch="feat/a", worktrees={"r": wt})}))
    _seed_canonical(tmp_path, "spec-canonical", body="Only in canonical store\n")
    try:
        result = runner.invoke(app, ["view", "spec"])
        assert result.exit_code == 0, result.output
        assert "Only in canonical store" in result.output
    finally:
        _reset_overrides()


def test_spec_cli_workitem_status_mutually_exclusive(tmp_path):
    runner = CliRunner()
    _setup_workspace(tmp_path)
    try:
        result = runner.invoke(app, ["view", "spec", "--workitem", "x", "--status", "y"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()
    finally:
        _reset_overrides()


def test_spec_cli_unknown_workitem_exits_1(tmp_path):
    runner = CliRunner()
    _setup_workspace(tmp_path)
    _seed_canonical(tmp_path, "spec-x", body="x\n")
    try:
        result = runner.invoke(app, ["view", "spec", "--workitem", "wi-missing"])
        assert result.exit_code == 1, result.output
        assert "wi-missing" in result.output
    finally:
        _reset_overrides()


@pytest.mark.asyncio
async def test_spec_view_renders_workitem_header(tmp_path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "s.md").write_text("# Hello\n\nBody text.\n")
    view = SpecView(
        workspace_root=tmp_path, name_or_path=None,
        header_provider=lambda: "◆ wi-1  ·  Overhaul  ·  [ready]",
        watch=False, interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "wi-1" in text and "Overhaul" in text
        assert "Body text" in text


# --- PR4: inline approve/request-changes on the spec stream view (AC7) ---


@pytest.mark.asyncio
async def test_spec_view_approve_writes_via_store(tmp_path):
    from mship.core.spec import AcceptanceCriterion, Spec
    from mship.core.spec_store import SPECS_DIRNAME, SpecStore
    store = SpecStore(tmp_path / SPECS_DIRNAME)
    p = store.save(Spec(id="spec-1", title="t", status="needs_review",
                        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                        updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc), body="# body\n",
                        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="approved")],
                        open_questions=[]))
    view = SpecView(workspace_root=tmp_path, name_or_path=str(p),
                    spec_store=store, spec_id="spec-1")
    async with view.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert store.find_by_id("spec-1").status == "approved"
        assert "approved" in view.last_action().lower()


# --- cockpit-v2 Task 7: spec --follow ---
from typer.testing import CliRunner as _CliRunner

from mship.cli import app as _app7, container as _c7
from mship.core.focus import focus_path, write_focus
from mship.core.spec import Spec as _Spec7
from mship.core.spec_store import SPECS_DIRNAME as _SPECS7, SpecStore as _SpecStore7
from mship.core.state import StateManager as _SM7, Task as _Task7, WorkspaceState as _WS7
from mship.core.workitem import WorkItem as _WI7
from mship.core.workitem_store import WorkItemStore as _WIS7


def _now_dt7():
    return datetime(2026, 7, 21, tzinfo=timezone.utc)


def _seed_follow_spec(tmp_path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir(exist_ok=True)
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    _SpecStore7(tmp_path / _SPECS7).save(_Spec7(
        id="spec-1", title="Overhaul", status="approved",
        created_at=_now_dt7(), updated_at=_now_dt7(), body="Overhaul body\n"))
    _WIS7(state_dir / "workitems").save(_WI7(
        id="wi-1", title="Overhaul", workspace="t", kind="feature",
        created_at=_now_dt7(), updated_at=_now_dt7(), spec_id="spec-1", task_slugs=["a"]))
    _SM7(state_dir).save(_WS7(tasks={"a": _Task7(
        slug="a", description="d", phase="dev", created_at=_now_dt7(),
        affected_repos=["r"], branch="feat/a", worktrees={"r": tmp_path}, work_item_id="wi-1")}))
    _c7.config.reset(); _c7.state_manager.reset()
    _c7.config_path.override(tmp_path / "mothership.yaml")
    _c7.state_dir.override(state_dir)
    return state_dir


def _reset_follow():
    _c7.config_path.reset_override(); _c7.state_dir.reset_override()
    _c7.config.reset_override(); _c7.config.reset()
    _c7.state_manager.reset_override(); _c7.state_manager.reset()


def test_spec_follow_no_focus_prints_hint(tmp_path):
    _seed_follow_spec(tmp_path)   # writes specs/<id>.md + workitem + state, binds container
    try:
        result = _CliRunner().invoke(_app7, ["view", "spec", "--follow"])
        assert result.exit_code == 0, result.output
        assert "no workitem focused" in result.output.lower()
    finally:
        _reset_follow()


def test_spec_follow_renders_focused_items_spec(tmp_path):
    state_dir = _seed_follow_spec(tmp_path)
    write_focus(focus_path(state_dir), "wi-1")
    try:
        result = _CliRunner().invoke(_app7, ["view", "spec", "--follow"])
        assert result.exit_code == 0, result.output
        assert "Overhaul body" in result.output   # the linked spec's body text
    finally:
        _reset_follow()


def test_spec_follow_conflicts_with_workitem(tmp_path):
    _seed_follow_spec(tmp_path)
    try:
        result = _CliRunner().invoke(_app7, ["view", "spec", "--follow", "--workitem", "wi-1"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output.lower()
    finally:
        _reset_follow()
