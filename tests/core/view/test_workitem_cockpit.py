from datetime import datetime, timezone
from pathlib import Path

from mship.core.message import Thread
from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, Spec
from mship.core.state import Task
from mship.core.workitem import WorkItem
from mship.core.view.workitem_index import build_workitem_index
from mship.core.view.workitem_cockpit import assemble_cockpit, render_text


def _now():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _spec():
    return Spec(
        id="spec-1", title="Overhaul spec", status="needs_review",
        created_at=_now(), updated_at=_now(),
        acceptance_criteria=[
            AcceptanceCriterion(
                id="ac1", text="does X",
                evidence=[AcceptanceEvidence(kind="test", ref="test-runs/1", note="green")]),
            AcceptanceCriterion(id="ac2", text="does Y"),
        ],
        body="b\n")


def _task():
    return Task(
        slug="a", description="d", phase="dev", created_at=_now(),
        affected_repos=["r"], branch="feat/a",
        worktrees={"r": Path("/tmp/wt-a")}, pr_urls={"r": "https://gh/pr/1"})


def _thread():
    return Thread(id="th-1", subject="Question about X",
                  created_at=_now(), updated_at=_now())


def _cockpit():
    spec, task, thread = _spec(), _task(), _thread()
    wi = WorkItem(id="wi-1", title="Overhaul", workspace="ws", kind="feature",
                  created_at=_now(), updated_at=_now(), spec_id="spec-1",
                  task_slugs=["a"], thread_ids=["th-1"])
    summary = build_workitem_index([wi], {"spec-1": spec}, {"a": task}, {"th-1": thread})[0]
    return assemble_cockpit(summary, spec, [task], [thread])


def test_cockpit_carries_spec_status_title_and_derived_phase():
    c = _cockpit()
    assert c.id == "wi-1" and c.title == "Overhaul" and c.kind == "feature"
    assert c.spec_id == "spec-1"
    assert c.spec_status == "needs_review"
    assert c.spec_title == "Overhaul spec"
    # needs_review spec (non-terminal) + one unfinished task -> in_flight.
    assert c.phase == "in_flight"


def test_cockpit_criteria_with_evidence():
    c = _cockpit()
    assert [x.id for x in c.criteria] == ["ac1", "ac2"]
    assert c.criteria[0].verdict == "unreviewed"
    assert c.criteria[0].evidence[0].ref == "test-runs/1"
    assert c.criteria[0].evidence[0].note == "green"
    assert c.criteria[1].evidence == []


def test_cockpit_tasks_carry_worktrees_and_branch():
    c = _cockpit()
    assert c.tasks[0].slug == "a"
    assert c.tasks[0].phase == "dev"
    assert c.tasks[0].worktrees == {"r": "/tmp/wt-a"}


def test_cockpit_prs_aggregated_from_task_pr_urls():
    c = _cockpit()
    assert len(c.prs) == 1
    assert c.prs[0].repo == "r"
    assert c.prs[0].task_slug == "a"
    assert c.prs[0].url == "https://gh/pr/1"


def test_cockpit_threads_carry_subject_and_flags():
    c = _cockpit()
    assert c.threads[0].id == "th-1"
    assert c.threads[0].subject == "Question about X"
    assert c.threads[0].needs_you is False


def test_cockpit_without_spec_is_safe():
    wi = WorkItem(id="wi-2", title="No spec", workspace="ws", kind="chore",
                  created_at=_now(), updated_at=_now())
    summary = build_workitem_index([wi], {}, {}, {})[0]
    c = assemble_cockpit(summary, None, [], [])
    assert c.spec_id is None and c.spec_status is None
    assert c.criteria == [] and c.tasks == [] and c.prs == [] and c.threads == []


def test_render_text_includes_every_section():
    txt = render_text(_cockpit())
    assert "wi-1" in txt and "Overhaul" in txt and "[in_flight]" in txt
    assert "needs_review" in txt
    assert "ac1" in txt and "green" in txt      # criterion + its evidence note
    assert "worktrees" in txt and "/tmp/wt-a" in txt
    assert "https://gh/pr/1" in txt
    assert "Question about X" in txt
