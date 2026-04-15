from datetime import datetime, timezone, timedelta
from pathlib import Path

from mship.core.state import Task, WorkspaceState, TestResult
from mship.core.view.task_index import TaskSummary, build_task_index


def _task(slug: str, **over) -> Task:
    base = dict(
        slug=slug,
        description=slug,
        phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"],
        worktrees={},
        branch=f"feat/{slug}",
    )
    base.update(over)
    return Task(**base)


def test_build_task_index_empty(tmp_path: Path):
    state = WorkspaceState()
    assert build_task_index(state, tmp_path) == []


def test_build_task_index_summarizes_active_task(tmp_path: Path):
    wt = tmp_path / "wt-a"
    wt.mkdir()
    (wt / "docs" / "superpowers" / "specs").mkdir(parents=True)
    (wt / "docs" / "superpowers" / "specs" / "s.md").write_text("# s")
    t = _task("a", worktrees={"mothership": wt})
    state = WorkspaceState(tasks={"a": t})

    [summary] = build_task_index(state, tmp_path)
    assert isinstance(summary, TaskSummary)
    assert summary.slug == "a"
    assert summary.phase == "dev"
    assert summary.affected_repos == ["mothership"]
    assert summary.worktrees == {"mothership": wt}
    assert summary.finished_at is None
    assert summary.spec_count == 1
    assert summary.orphan is False
    assert summary.tests_failing is False


def test_build_task_index_flags_orphan_worktree(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    t = _task("a", worktrees={"mothership": missing})
    state = WorkspaceState(tasks={"a": t})
    [summary] = build_task_index(state, tmp_path)
    assert summary.orphan is True


def test_build_task_index_flags_tests_failing(tmp_path: Path):
    t = _task("a", test_results={"mothership": TestResult(status="fail", at=datetime.now(timezone.utc))})
    state = WorkspaceState(tasks={"a": t})
    [summary] = build_task_index(state, tmp_path)
    assert summary.tests_failing is True


def test_build_task_index_orders_active_before_finished(tmp_path: Path):
    now = datetime.now(timezone.utc)
    active = _task("active", created_at=now - timedelta(hours=1))
    finished = _task("finished", created_at=now - timedelta(hours=2), finished_at=now)
    state = WorkspaceState(tasks={"finished": finished, "active": active})
    slugs = [s.slug for s in build_task_index(state, tmp_path)]
    assert slugs == ["active", "finished"]


def test_build_task_index_orders_active_by_created_desc(tmp_path: Path):
    now = datetime.now(timezone.utc)
    older = _task("older", created_at=now - timedelta(hours=2))
    newer = _task("newer", created_at=now - timedelta(minutes=5))
    state = WorkspaceState(tasks={"older": older, "newer": newer})
    assert [s.slug for s in build_task_index(state, tmp_path)] == ["newer", "older"]
