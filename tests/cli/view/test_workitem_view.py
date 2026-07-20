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
