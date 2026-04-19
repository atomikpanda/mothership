"""Tests for `_build_pr_groups` — pure grouping logic for mship finish.

Shared-git_root repos that push to the same branch resolve to one gh PR.
This helper groups them so finish can make one push + one create call
instead of one per repo.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.state import Task


def _cfg(**repos: RepoConfig) -> WorkspaceConfig:
    return WorkspaceConfig(workspace="t", repos=dict(repos))


def _task(affected: list[str], branch: str = "feat/x") -> Task:
    return Task(
        slug="t", description="t", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=affected, worktrees={},
        branch=branch, base_branch="main",
    )


def test_three_repos_shared_git_root_form_one_group(tmp_path: Path):
    from mship.cli.worktree import _build_pr_groups
    config = _cfg(
        tailrd=RepoConfig(path=tmp_path / "tailrd", type="service"),
        infra=RepoConfig(path=Path("."), git_root="tailrd", type="service"),
        web=RepoConfig(path=Path("web"), git_root="tailrd", type="service"),
    )
    task = _task(["infra", "tailrd", "web"])
    effective_bases = {"infra": "main", "tailrd": "main", "web": "main"}

    groups = _build_pr_groups(
        ["infra", "tailrd", "web"], config, task, effective_bases,
    )
    assert len(groups) == 1
    g = groups[0]
    assert sorted(g.members) == ["infra", "tailrd", "web"]
    assert g.rep_name == "tailrd"  # git_root parent preferred
    assert g.base == "main"


def test_two_shared_plus_one_standalone_form_two_groups(tmp_path: Path):
    from mship.cli.worktree import _build_pr_groups
    config = _cfg(
        tailrd=RepoConfig(path=tmp_path / "tailrd", type="service"),
        infra=RepoConfig(path=Path("."), git_root="tailrd", type="service"),
        api=RepoConfig(path=tmp_path / "api", type="service"),
    )
    task = _task(["infra", "tailrd", "api"])
    effective_bases = {"infra": "main", "tailrd": "main", "api": "main"}

    groups = _build_pr_groups(
        ["infra", "tailrd", "api"], config, task, effective_bases,
    )
    assert len(groups) == 2
    by_rep = {g.rep_name: g for g in groups}
    assert sorted(by_rep["tailrd"].members) == ["infra", "tailrd"]
    assert by_rep["api"].members == ["api"]


def test_all_standalone_form_n_groups(tmp_path: Path):
    from mship.cli.worktree import _build_pr_groups
    config = _cfg(
        api=RepoConfig(path=tmp_path / "api", type="service"),
        web=RepoConfig(path=tmp_path / "web", type="service"),
    )
    task = _task(["api", "web"])
    effective_bases = {"api": "main", "web": "main"}

    groups = _build_pr_groups(["api", "web"], config, task, effective_bases)
    assert len(groups) == 2
    assert sorted(g.rep_name for g in groups) == ["api", "web"]


def test_git_root_parent_not_in_affected_repos_falls_back_to_first_member(tmp_path: Path):
    """Pathological: user passes --repos that excludes the git_root parent."""
    from mship.cli.worktree import _build_pr_groups
    config = _cfg(
        tailrd=RepoConfig(path=tmp_path / "tailrd", type="service"),
        infra=RepoConfig(path=Path("."), git_root="tailrd", type="service"),
        web=RepoConfig(path=Path("web"), git_root="tailrd", type="service"),
    )
    task = _task(["infra", "web"])  # tailrd not included
    effective_bases = {"infra": "main", "web": "main"}

    groups = _build_pr_groups(["infra", "web"], config, task, effective_bases)
    assert len(groups) == 1
    g = groups[0]
    # Parent not in affected; representative falls back to first member in input order.
    assert g.rep_name == "infra"


def test_heterogeneous_bases_within_group_raises(tmp_path: Path):
    """Defensive: if shared-git_root members somehow have different bases,
    we surface an error rather than pick one silently."""
    from mship.cli.worktree import _build_pr_groups
    config = _cfg(
        tailrd=RepoConfig(path=tmp_path / "tailrd", type="service"),
        infra=RepoConfig(path=Path("."), git_root="tailrd", type="service"),
    )
    task = _task(["infra", "tailrd"])
    effective_bases = {"infra": "main", "tailrd": "develop"}  # mismatch

    with pytest.raises(ValueError, match="mixed effective_bases"):
        _build_pr_groups(["infra", "tailrd"], config, task, effective_bases)


def test_group_rep_path_uses_git_root_effective_path(tmp_path: Path):
    """Group's rep_path should be the parent's effective path, not a subdir."""
    from mship.cli.worktree import _build_pr_groups
    parent_path = tmp_path / "tailrd"
    parent_path.mkdir()
    config = _cfg(
        tailrd=RepoConfig(path=parent_path, type="service"),
        web=RepoConfig(path=Path("web"), git_root="tailrd", type="service"),
    )
    task = _task(["tailrd", "web"])
    effective_bases = {"tailrd": "main", "web": "main"}

    groups = _build_pr_groups(["tailrd", "web"], config, task, effective_bases)
    assert len(groups) == 1
    # Representative is tailrd, rep_path = tailrd's effective path (not web subdir).
    assert groups[0].rep_path == parent_path.resolve()
