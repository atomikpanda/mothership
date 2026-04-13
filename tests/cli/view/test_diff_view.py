import subprocess
from pathlib import Path

import pytest

from mship.cli.view.diff import DiffView


def _init_repo(path: Path, seed: str = "seed\n") -> None:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "seed.txt").write_text(seed)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_diff_view_shows_untracked_and_modified(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\nmore\n")
    (tmp_path / "new.py").write_text("print('hi')\n")

    view = DiffView(worktree_paths=[tmp_path], watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "+more" in text
        assert "+print('hi')" in text


@pytest.mark.asyncio
async def test_diff_view_clean_worktree_shows_clean_marker(tmp_path: Path):
    _init_repo(tmp_path)
    view = DiffView(worktree_paths=[tmp_path], watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "clean" in view.rendered_text().lower()


@pytest.mark.asyncio
async def test_diff_view_multiple_worktrees_show_headers(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _init_repo(a)
    _init_repo(b)
    (a / "x.txt").write_text("x")
    view = DiffView(worktree_paths=[a, b], watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert str(a) in text
        assert str(b) in text
