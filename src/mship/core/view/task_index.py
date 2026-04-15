"""Task-index data layer for the cross-task `mship view` God view."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mship.core.state import Task, WorkspaceState


@dataclass(frozen=True)
class TaskSummary:
    slug: str
    phase: str
    branch: str
    affected_repos: list[str]
    worktrees: dict[str, Path]
    finished_at: datetime | None
    blocked_reason: str | None
    created_at: datetime
    spec_count: int
    orphan: bool
    tests_failing: bool


def _summarize(task: Task) -> TaskSummary:
    worktrees = {repo: Path(p) for repo, p in task.worktrees.items()}
    orphan = any(not p.exists() for p in worktrees.values())
    spec_count = 0
    for p in worktrees.values():
        specs_dir = p / "docs" / "superpowers" / "specs"
        if specs_dir.is_dir():
            spec_count += sum(1 for f in specs_dir.iterdir() if f.is_file() and f.suffix == ".md")
    tests_failing = any(r.status == "fail" for r in task.test_results.values())
    return TaskSummary(
        slug=task.slug,
        phase=task.phase,
        branch=task.branch,
        affected_repos=list(task.affected_repos),
        worktrees=worktrees,
        finished_at=task.finished_at,
        blocked_reason=task.blocked_reason,
        created_at=task.created_at,
        spec_count=spec_count,
        orphan=orphan,
        tests_failing=tests_failing,
    )


def build_task_index(state: WorkspaceState, workspace_root: Path) -> list[TaskSummary]:
    """Active tasks first (by created_at desc), then finished-awaiting-close (also desc)."""
    summaries = [_summarize(t) for t in state.tasks.values()]
    active = sorted(
        [s for s in summaries if s.finished_at is None],
        key=lambda s: s.created_at, reverse=True,
    )
    finished = sorted(
        [s for s in summaries if s.finished_at is not None],
        key=lambda s: s.created_at, reverse=True,
    )
    return active + finished
