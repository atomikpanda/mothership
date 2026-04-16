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


from mship.core.view.task_index import SpecEntry, find_all_specs


def _write_spec(path: Path, body: str = "# Title\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_find_all_specs_scans_each_worktree(tmp_path: Path):
    wt_a = tmp_path / "wt-a"
    wt_b = tmp_path / "wt-b"
    _write_spec(wt_a / "docs" / "superpowers" / "specs" / "a.md", "# Alpha\n")
    _write_spec(wt_b / "docs" / "superpowers" / "specs" / "b.md", "# Beta\n")
    state = WorkspaceState(tasks={
        "a": _task("a", worktrees={"mothership": wt_a}),
        "b": _task("b", worktrees={"mothership": wt_b}),
    })
    specs = find_all_specs(state, tmp_path)
    titles = {(s.task_slug, s.path.name, s.title) for s in specs}
    assert ("a", "a.md", "Alpha") in titles
    assert ("b", "b.md", "Beta") in titles


def test_find_all_specs_includes_main_checkout_with_none_slug(tmp_path: Path):
    _write_spec(tmp_path / "docs" / "superpowers" / "specs" / "legacy.md", "# Legacy\n")
    state = WorkspaceState()
    specs = find_all_specs(state, tmp_path)
    assert [(s.task_slug, s.path.name) for s in specs] == [(None, "legacy.md")]


def test_find_all_specs_title_falls_back_to_stem(tmp_path: Path):
    _write_spec(tmp_path / "docs" / "superpowers" / "specs" / "untitled.md", "no heading here\n")
    [entry] = find_all_specs(WorkspaceState(), tmp_path)
    assert entry.title == "untitled"


def test_find_all_specs_empty(tmp_path: Path):
    assert find_all_specs(WorkspaceState(), tmp_path) == []


def test_find_all_specs_sorted_flat_by_mtime_desc_across_tasks(tmp_path: Path):
    """Newer spec from a finished task must appear before an older spec from an active task."""
    import os
    now = datetime.now(timezone.utc)

    wt_active = tmp_path / "wt-active"
    wt_finished = tmp_path / "wt-finished"
    old_spec = wt_active / "docs" / "superpowers" / "specs" / "older.md"
    new_spec = wt_finished / "docs" / "superpowers" / "specs" / "newer.md"
    _write_spec(old_spec, "# Older\n")
    _write_spec(new_spec, "# Newer\n")

    # Force mtimes: new_spec newer than old_spec.
    os.utime(old_spec, (1_000_000_000, 1_000_000_000))
    os.utime(new_spec, (2_000_000_000, 2_000_000_000))

    state = WorkspaceState(tasks={
        "active": _task("active", created_at=now, worktrees={"mothership": wt_active}),
        "finished": _task(
            "finished", created_at=now - timedelta(hours=2),
            finished_at=now, worktrees={"mothership": wt_finished},
        ),
    })
    specs = find_all_specs(state, tmp_path)
    names = [s.path.name for s in specs]
    assert names.index("newer.md") < names.index("older.md")
