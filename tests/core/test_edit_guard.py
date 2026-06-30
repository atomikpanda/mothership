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


def test_nested_repo_without_task_allowed_even_if_parent_has_task(tmp_path: Path):
    # A child repo nested inside a parent repo's path: the parent has an active
    # task, the child does not. Editing a child file must NOT be falsely denied
    # as the parent's main checkout — the most-specific (deepest) repo owns the
    # file, independent of config declaration order.
    parent = tmp_path / "ws"; (parent / "src").mkdir(parents=True)
    child = parent / "a"; (child / "src").mkdir(parents=True)
    parent_wt = tmp_path / ".worktrees" / "p" / "parent"; parent_wt.mkdir(parents=True)
    cfg = _Config({"parent": parent, "child": child})  # parent first = the failing order
    state = _state("p", "parent", parent_wt)            # only parent has a task
    assert evaluate_edit(child / "src" / "x.py", state, cfg).allowed is True


def test_parent_repo_own_file_still_blocked_with_nested_child(tmp_path: Path):
    # Editing the parent's OWN file (not inside the child) while the parent has a
    # task is still blocked and names the parent.
    parent = tmp_path / "ws"; (parent / "src").mkdir(parents=True)
    child = parent / "a"; (child / "src").mkdir(parents=True)
    parent_wt = tmp_path / ".worktrees" / "p" / "parent"; parent_wt.mkdir(parents=True)
    cfg = _Config({"parent": parent, "child": child})
    state = _state("p", "parent", parent_wt)
    d = evaluate_edit(parent / "src" / "x.py", state, cfg)
    assert d.allowed is False
    assert "parent" in d.reason


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


def test_multi_task_block_lists_all_candidate_worktrees(tmp_path: Path):
    from datetime import datetime, timezone
    main = tmp_path / "main"; (main / "src").mkdir(parents=True)
    wt1 = tmp_path / ".worktrees" / "t1" / "repo"; (wt1 / "src").mkdir(parents=True)
    wt2 = tmp_path / ".worktrees" / "t2" / "repo"; (wt2 / "src").mkdir(parents=True)
    cfg = _Config({"repo": main})
    def _t(slug, wt):
        return Task(slug=slug, description="d", phase="dev",
                    created_at=datetime.now(timezone.utc),
                    affected_repos=["repo"], worktrees={"repo": wt}, branch=f"feat/{slug}")
    state = WorkspaceState(tasks={"t1": _t("t1", wt1), "t2": _t("t2", wt2)})
    d = evaluate_edit(main / "src" / "x.py", state, cfg)
    assert d.allowed is False
    # Both task slugs and both worktree paths appear, not just the first.
    assert "t1" in d.reason and "t2" in d.reason
    assert str(wt1 / "src" / "x.py") in d.reason
    assert str(wt2 / "src" / "x.py") in d.reason


def test_multi_task_allows_edit_inside_one_of_the_worktrees(tmp_path: Path):
    from datetime import datetime, timezone
    main = tmp_path / "main"; (main / "src").mkdir(parents=True)
    wt1 = tmp_path / ".worktrees" / "t1" / "repo"; (wt1 / "src").mkdir(parents=True)
    wt2 = tmp_path / ".worktrees" / "t2" / "repo"; (wt2 / "src").mkdir(parents=True)
    cfg = _Config({"repo": main})
    def _t(slug, wt):
        return Task(slug=slug, description="d", phase="dev",
                    created_at=datetime.now(timezone.utc),
                    affected_repos=["repo"], worktrees={"repo": wt}, branch=f"feat/{slug}")
    state = WorkspaceState(tasks={"t1": _t("t1", wt1), "t2": _t("t2", wt2)})
    assert evaluate_edit(wt2 / "src" / "x.py", state, cfg).allowed is True
