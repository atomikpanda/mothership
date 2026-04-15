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


@dataclass(frozen=True)
class SpecEntry:
    task_slug: str | None           # None == main checkout (legacy)
    path: Path
    mtime: float
    title: str


_SPEC_SUBDIR = Path("docs") / "superpowers" / "specs"


def _extract_title(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(2048)
    except OSError:
        return path.stem
    for line in head.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or path.stem
    return path.stem


def _scan_specs_dir(specs_dir: Path, task_slug: str | None) -> list[SpecEntry]:
    if not specs_dir.is_dir():
        return []
    out: list[SpecEntry] = []
    for f in specs_dir.iterdir():
        if not f.is_file() or f.suffix != ".md":
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        out.append(SpecEntry(task_slug=task_slug, path=f, mtime=mtime, title=_extract_title(f)))
    return out


def find_all_specs(state: WorkspaceState, workspace_root: Path) -> list[SpecEntry]:
    """All specs across every task's worktrees + the main checkout.

    Grouped by task (active first, finished last, None/main first within the
    None group); within each group, newest mtime first.
    """
    index = build_task_index(state, workspace_root)
    entries: list[SpecEntry] = []
    seen_paths: set[Path] = set()

    # Main-checkout specs get task_slug=None.
    main_entries = sorted(
        _scan_specs_dir(workspace_root / _SPEC_SUBDIR, None),
        key=lambda e: e.mtime, reverse=True,
    )
    for e in main_entries:
        if e.path not in seen_paths:
            entries.append(e)
            seen_paths.add(e.path)

    for summary in index:
        per_task: list[SpecEntry] = []
        for wt in summary.worktrees.values():
            per_task.extend(_scan_specs_dir(wt / _SPEC_SUBDIR, summary.slug))
        per_task.sort(key=lambda e: e.mtime, reverse=True)
        for e in per_task:
            if e.path not in seen_paths:
                entries.append(e)
                seen_paths.add(e.path)
    return entries
