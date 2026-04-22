"""Unit tests for the workspace_marker module. See #84."""
from pathlib import Path

import pytest


def _write_yaml(path: Path, name: str = "t") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "mothership.yaml").write_text(f"workspace: {name}\nrepos: {{}}\n")


def test_write_marker_creates_file(tmp_path: Path):
    from mship.core.workspace_marker import write_marker, MARKER_NAME
    worktree = tmp_path / "wt"; worktree.mkdir()
    root = tmp_path / "ws"; _write_yaml(root)
    write_marker(worktree, root)
    marker = worktree / MARKER_NAME
    assert marker.exists()
    assert marker.read_text().strip() == str(root.resolve())


def test_write_marker_overwrites_existing(tmp_path: Path):
    from mship.core.workspace_marker import write_marker, MARKER_NAME
    worktree = tmp_path / "wt"; worktree.mkdir()
    (worktree / MARKER_NAME).write_text("/stale/path\n")
    root = tmp_path / "ws"; _write_yaml(root)
    write_marker(worktree, root)
    assert (worktree / MARKER_NAME).read_text().strip() == str(root.resolve())


def test_read_marker_from_ancestor_immediate(tmp_path: Path):
    from mship.core.workspace_marker import read_marker_from_ancestor, write_marker
    worktree = tmp_path / "wt"; worktree.mkdir()
    root = tmp_path / "ws"; _write_yaml(root)
    write_marker(worktree, root)
    assert read_marker_from_ancestor(worktree) == root.resolve()


def test_read_marker_from_ancestor_walks_up(tmp_path: Path):
    from mship.core.workspace_marker import read_marker_from_ancestor, write_marker
    worktree = tmp_path / "wt"; worktree.mkdir()
    nested = worktree / "a" / "b" / "c"; nested.mkdir(parents=True)
    root = tmp_path / "ws"; _write_yaml(root)
    write_marker(worktree, root)
    assert read_marker_from_ancestor(nested) == root.resolve()


def test_read_marker_returns_none_when_absent(tmp_path: Path):
    from mship.core.workspace_marker import read_marker_from_ancestor
    here = tmp_path / "anywhere"; here.mkdir()
    assert read_marker_from_ancestor(here) is None


def test_read_marker_stale_missing_dir_returns_none(tmp_path: Path):
    """Marker points to a dir that doesn't exist → treated as absent."""
    from mship.core.workspace_marker import read_marker_from_ancestor, MARKER_NAME
    worktree = tmp_path / "wt"; worktree.mkdir()
    (worktree / MARKER_NAME).write_text(str(tmp_path / "does-not-exist"))
    assert read_marker_from_ancestor(worktree) is None


def test_read_marker_stale_no_yaml_returns_none(tmp_path: Path):
    """Marker points to an existing dir that has no mothership.yaml → None."""
    from mship.core.workspace_marker import read_marker_from_ancestor, MARKER_NAME
    worktree = tmp_path / "wt"; worktree.mkdir()
    other = tmp_path / "other-dir"; other.mkdir()
    (worktree / MARKER_NAME).write_text(str(other))
    assert read_marker_from_ancestor(worktree) is None


def test_append_to_worktree_exclude_creates_line(tmp_path: Path):
    from mship.core.workspace_marker import (
        append_to_worktree_exclude, MARKER_NAME,
    )
    parent_git = tmp_path / "parent-git"
    wt_info = parent_git / "worktrees" / "my-slug" / "info"
    wt_info.mkdir(parents=True)
    (wt_info / "exclude").write_text("# existing\n*.pyc\n")
    worktree = tmp_path / "wt"; worktree.mkdir()
    ok = append_to_worktree_exclude(worktree, parent_git, "my-slug")
    assert ok is True
    content = (wt_info / "exclude").read_text()
    assert MARKER_NAME in content
    assert "*.pyc" in content  # existing lines preserved


def test_append_to_worktree_exclude_idempotent(tmp_path: Path):
    from mship.core.workspace_marker import (
        append_to_worktree_exclude, MARKER_NAME,
    )
    parent_git = tmp_path / "parent-git"
    wt_info = parent_git / "worktrees" / "s" / "info"
    wt_info.mkdir(parents=True)
    (wt_info / "exclude").write_text(f"{MARKER_NAME}\n")
    worktree = tmp_path / "wt"; worktree.mkdir()
    ok = append_to_worktree_exclude(worktree, parent_git, "s")
    assert ok is True
    # Line appears exactly once.
    lines = [l for l in (wt_info / "exclude").read_text().splitlines() if l == MARKER_NAME]
    assert len(lines) == 1


def test_append_to_worktree_exclude_missing_dir_returns_false(tmp_path: Path):
    """When parent-git/worktrees/<slug>/info doesn't exist, return False gracefully."""
    from mship.core.workspace_marker import append_to_worktree_exclude
    parent_git = tmp_path / "parent-git"; parent_git.mkdir()
    worktree = tmp_path / "wt"; worktree.mkdir()
    assert append_to_worktree_exclude(worktree, parent_git, "absent-slug") is False
