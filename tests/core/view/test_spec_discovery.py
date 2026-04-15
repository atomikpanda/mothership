import os
import time
from pathlib import Path
import pytest

from mship.core.view.spec_discovery import find_spec, SpecNotFoundError


def _touch(path: Path, mtime: float) -> None:
    path.write_text("# test\n")
    os.utime(path, (mtime, mtime))


def test_find_newest_by_mtime(tmp_path: Path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    _touch(specs / "2026-04-11-a.md", time.time() - 100)
    _touch(specs / "2026-04-12-b.md", time.time() - 10)
    _touch(specs / "2026-04-10-c.md", time.time() - 200)

    result = find_spec(workspace_root=tmp_path, name_or_path=None)
    assert result.name == "2026-04-12-b.md"


def test_find_by_name_with_extension(tmp_path: Path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "foo.md").write_text("# foo")
    result = find_spec(workspace_root=tmp_path, name_or_path="foo.md")
    assert result.name == "foo.md"


def test_find_by_name_without_extension(tmp_path: Path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "foo.md").write_text("# foo")
    result = find_spec(workspace_root=tmp_path, name_or_path="foo")
    assert result.name == "foo.md"


def test_find_by_absolute_path(tmp_path: Path):
    f = tmp_path / "custom.md"
    f.write_text("# custom")
    result = find_spec(workspace_root=tmp_path, name_or_path=str(f))
    assert result == f


def test_absolute_path_missing_raises(tmp_path: Path):
    with pytest.raises(SpecNotFoundError):
        find_spec(workspace_root=tmp_path, name_or_path=str(tmp_path / "nope.md"))


def test_empty_specs_dir_raises(tmp_path: Path):
    (tmp_path / "docs" / "superpowers" / "specs").mkdir(parents=True)
    with pytest.raises(SpecNotFoundError):
        find_spec(workspace_root=tmp_path, name_or_path=None)


def test_missing_specs_dir_raises(tmp_path: Path):
    with pytest.raises(SpecNotFoundError):
        find_spec(workspace_root=tmp_path, name_or_path=None)


def test_missing_named_spec_raises(tmp_path: Path):
    (tmp_path / "docs" / "superpowers" / "specs").mkdir(parents=True)
    with pytest.raises(SpecNotFoundError):
        find_spec(workspace_root=tmp_path, name_or_path="does-not-exist")


from mship.core.state import WorkspaceState, Task
from datetime import datetime, timezone


def _make_task(slug: str, worktree: Path) -> Task:
    return Task(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"],
        worktrees={"mothership": worktree},
        branch=f"feat/{slug}",
    )


def test_find_spec_with_task_returns_newest_in_task_worktree(tmp_path: Path):
    wt = tmp_path / "wt-a"
    specs = wt / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    _touch(specs / "old.md", time.time() - 100)
    _touch(specs / "new.md", time.time() - 5)
    state = WorkspaceState(tasks={"a": _make_task("a", wt)})

    result = find_spec(workspace_root=tmp_path, name_or_path=None, task="a", state=state)
    assert result.name == "new.md"


def test_find_spec_unknown_task_raises(tmp_path: Path):
    state = WorkspaceState()
    with pytest.raises(SpecNotFoundError):
        find_spec(workspace_root=tmp_path, name_or_path=None, task="nope", state=state)


def test_find_spec_by_name_searches_all_worktrees_when_no_task(tmp_path: Path):
    wt = tmp_path / "wt-a"
    specs = wt / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "found.md").write_text("# found\n")
    state = WorkspaceState(tasks={"a": _make_task("a", wt)})

    result = find_spec(workspace_root=tmp_path, name_or_path="found", state=state)
    assert result == specs / "found.md"
