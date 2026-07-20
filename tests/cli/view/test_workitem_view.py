import pytest

from mship.core.spec import AcceptanceEvidence
from mship.core.view.workitem_cockpit import (
    CriterionView, PRView, TaskView, ThreadView, WorkItemCockpit)
from mship.cli.view.workitem import WorkItemCockpitView


def _cockpit():
    return WorkItemCockpit(
        id="wi-1", title="Overhaul", kind="feature", phase="in_flight",
        spec_id="spec-1", spec_title="Overhaul spec", spec_status="needs_review",
        criteria=[CriterionView(
            id="ac1", text="does X", verdict="unreviewed",
            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/1", note="green")])],
        tasks=[TaskView(slug="a", phase="dev", branch="feat/a",
                        worktrees={"r": "/tmp/wt-a"}, pr_urls={"r": "https://gh/pr/1"},
                        blocked_reason=None, finished_at=None)],
        prs=[PRView(task_slug="a", repo="r", url="https://gh/pr/1")],
        threads=[ThreadView(id="th-1", subject="Question about X",
                            needs_you=False, needs_decision=False, unseen=False)],
    )


@pytest.mark.asyncio
async def test_cockpit_view_lists_all_entities_with_header():
    view = WorkItemCockpitView(_cockpit())
    async with view.run_test() as pilot:
        await pilot.pause()
        labels = view.list_labels()
        assert any(l.startswith("spec") for l in labels)
        assert any("ac1" in l for l in labels)
        assert any(l.startswith("task") for l in labels)
        assert any(l.startswith("PR") for l in labels)
        assert any("thread" in l for l in labels)
        assert "wi-1" in view.header_text() and "Overhaul" in view.header_text()
        # First row (spec) detail shows status + WorkItem phase.
        assert "needs_review" in view.detail_text()
        assert "in_flight" in view.detail_text()


@pytest.mark.asyncio
async def test_cockpit_view_drills_show_criterion_evidence_and_worktrees():
    view = WorkItemCockpitView(_cockpit())
    async with view.run_test() as pilot:
        await pilot.pause()
        view._master.focus()
        await pilot.pause()
        # Row order: spec(0), ac1(1), task(2), PR(3), thread(4).
        await pilot.press("j")  # -> ac1
        await pilot.pause()
        assert "ac1" in view.detail_text()
        assert "green" in view.detail_text()      # evidence note surfaced
        await pilot.press("j")  # -> task
        await pilot.pause()
        assert "worktrees" in view.detail_text()
        assert "/tmp/wt-a" in view.detail_text()


# --- CLI: mship view workitem <id> ---
from datetime import datetime, timezone

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.message import Thread
from mship.core.message_store import MessageStore
from mship.core.spec import AcceptanceCriterion, Spec
from mship.core.spec_store import SPECS_DIRNAME, SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem import WorkItem
from mship.core.workitem_store import WorkItemStore


def _dt():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _seed_workspace(tmp_path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")

    SpecStore(tmp_path / SPECS_DIRNAME).save(Spec(
        id="spec-1", title="Overhaul spec", status="needs_review",
        created_at=_dt(), updated_at=_dt(),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="does X")],
        body="b\n"))
    WorkItemStore(state_dir / "workitems").save(WorkItem(
        id="wi-1", title="Overhaul", workspace="t", kind="feature",
        created_at=_dt(), updated_at=_dt(), spec_id="spec-1",
        task_slugs=["a"], thread_ids=["th-1"]))
    MessageStore(state_dir / "messages").save(Thread(
        id="th-1", subject="Question about X", created_at=_dt(), updated_at=_dt()))
    StateManager(state_dir).save(WorkspaceState(tasks={"a": Task(
        slug="a", description="d", phase="dev", created_at=_dt(),
        affected_repos=["r"], branch="feat/a",
        worktrees={"r": tmp_path / "wt-a"}, pr_urls={"r": "https://gh/pr/1"},
        work_item_id="wi-1")}))

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_workitem_registered_in_view_help():
    result = CliRunner().invoke(app, ["view", "--help"])
    assert result.exit_code == 0
    assert "workitem" in result.stdout


def test_workitem_cli_renders_cockpit_text(tmp_path):
    _seed_workspace(tmp_path)
    try:
        # CliRunner stdout is not a TTY -> non-TTY text short-circuit (no TUI hang).
        result = CliRunner().invoke(app, ["view", "workitem", "wi-1"])
        assert result.exit_code == 0, result.output
        assert "wi-1" in result.output and "Overhaul" in result.output
        assert "needs_review" in result.output
        assert "ac1" in result.output
        assert "worktrees" in result.output
        assert "https://gh/pr/1" in result.output
        assert "Question about X" in result.output
    finally:
        _reset()


def test_workitem_cli_unknown_id_exits_1(tmp_path):
    _seed_workspace(tmp_path)
    try:
        result = CliRunner().invoke(app, ["view", "workitem", "wi-missing"])
        assert result.exit_code == 1
        assert "wi-missing" in result.output
    finally:
        _reset()
