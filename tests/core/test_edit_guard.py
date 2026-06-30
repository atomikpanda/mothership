from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mship.core.edit_guard import GuardDecision, evaluate_edit
from mship.core.state import Task, WorkspaceState


class _Repo:
    def __init__(self, path: Path):
        self.path = path


class _Config:
    def __init__(self, repos: dict[str, Path]):
        self.repos = {name: _Repo(p) for name, p in repos.items()}


def _state(slug: str, repo: str, worktree: Path) -> WorkspaceState:
    t = Task(
        slug=slug, description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=[repo], worktrees={repo: worktree},
        branch=f"feat/{slug}",
    )
    return WorkspaceState(tasks={slug: t})


def _layout(tmp_path: Path):
    main = tmp_path / "main"; (main / "src").mkdir(parents=True)
    wt = tmp_path / ".worktrees" / "t" / "repo" / "src"
    wt.mkdir(parents=True)
    return main, wt.parent


def test_blocks_edit_in_main_checkout_while_task_active(tmp_path: Path):
    main, wt = _layout(tmp_path)
    cfg = _Config({"repo": main})
    state = _state("t", "repo", wt)
    d = evaluate_edit(main / "src" / "x.py", state, cfg)
    assert d.allowed is False
    assert "MAIN checkout" in d.reason
    assert "repo" in d.reason and "t" in d.reason
    assert str(wt / "src" / "x.py") in d.reason


def test_allows_edit_in_worktree(tmp_path: Path):
    main, wt = _layout(tmp_path)
    cfg = _Config({"repo": main})
    state = _state("t", "repo", wt)
    assert evaluate_edit(wt / "src" / "x.py", state, cfg) == GuardDecision(allowed=True)


def test_allows_when_repo_has_no_active_task(tmp_path: Path):
    main, _ = _layout(tmp_path)
    cfg = _Config({"repo": main})
    state = WorkspaceState(tasks={})
    assert evaluate_edit(main / "src" / "x.py", state, cfg).allowed is True


def test_allows_edit_outside_any_repo(tmp_path: Path):
    main, wt = _layout(tmp_path)
    cfg = _Config({"repo": main})
    state = _state("t", "repo", wt)
    assert evaluate_edit(tmp_path / "specs" / "a.md", state, cfg).allowed is True


def test_blocks_via_symlink_to_main_checkout(tmp_path: Path):
    main, wt = _layout(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(main, target_is_directory=True)
    cfg = _Config({"repo": main})
    state = _state("t", "repo", wt)
    assert evaluate_edit(link / "src" / "x.py", state, cfg).allowed is False


def test_blocks_new_file_that_does_not_exist_yet(tmp_path: Path):
    main, wt = _layout(tmp_path)
    cfg = _Config({"repo": main})
    state = _state("t", "repo", wt)
    assert evaluate_edit(main / "src" / "brand_new.py", state, cfg).allowed is False
