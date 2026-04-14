import os
import subprocess
from pathlib import Path

import pytest

from mship.cli.view.diff import DiffView
from mship.core.view.diff_sources import FileDiff, WorktreeDiff


def _fd(path: str, body: str = "", additions: int = 1, deletions: int = 0) -> FileDiff:
    return FileDiff(path=path, additions=additions, deletions=deletions, body=body)


def _wd(root: Path, files: list[FileDiff]) -> WorktreeDiff:
    return WorktreeDiff(root=root, files=tuple(files))


def _seed(view: DiffView, mapping: dict[Path, list[FileDiff]]) -> None:
    """Inject fake worktree diffs so tests don't shell out to git."""
    view._test_override = {p: _wd(p, files) for p, files in mapping.items()}


@pytest.mark.asyncio
async def test_tree_populated_from_worktrees(tmp_path):
    wa = tmp_path / "a"
    wb = tmp_path / "b"
    view = DiffView(worktree_paths=[wa, wb], use_delta=False, watch=False, interval=1.0)
    _seed(view, {
        wa: [_fd("a.py", "diff --git a/a.py b/a.py\n+++ b/a.py\n+x\n"),
              _fd("b.py", "diff --git a/b.py b/b.py\n+++ b/b.py\n+y\n")],
        wb: [_fd("c.py", "diff --git a/c.py b/c.py\n+++ b/c.py\n+z\n")],
    })
    async with view.run_test() as pilot:
        await pilot.pause()
        labels = view.tree_labels()
        assert any(str(wa) in l for l in labels)
        assert any(str(wb) in l for l in labels)
        assert any("a.py" in l for l in labels)
        assert any("c.py" in l for l in labels)


@pytest.mark.asyncio
async def test_first_file_selected_on_mount(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=1.0)
    _seed(view, {wa: [_fd("first.py", "diff --git a/first.py b/first.py\n+++ b/first.py\n+one\n")]})
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "one" in view.diff_text()


@pytest.mark.asyncio
async def test_large_worktree_starts_collapsed(tmp_path):
    wa = tmp_path / "a"
    wb = tmp_path / "b"
    view = DiffView(worktree_paths=[wa, wb], use_delta=False, watch=False, interval=1.0)
    many = [_fd(f"f{i}.py", f"diff --git a/f{i}.py b/f{i}.py\n+++ b/f{i}.py\n+x\n") for i in range(25)]
    few = [_fd("only.py", "diff --git a/only.py b/only.py\n+++ b/only.py\n+x\n")]
    _seed(view, {wa: many, wb: few})
    async with view.run_test() as pilot:
        await pilot.pause()
        assert view.is_worktree_collapsed(wa) is True
        assert view.is_worktree_collapsed(wb) is False


@pytest.mark.asyncio
async def test_lockfile_collapsed_by_default(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=1.0)
    lock_body = ("diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n"
                 "+++ b/pnpm-lock.yaml\n"
                 "+noisy noisy noisy\n")
    _seed(view, {wa: [_fd("pnpm-lock.yaml", lock_body, additions=1)]})
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "collapsed" in view.diff_text()
        assert "noisy noisy noisy" not in view.diff_text()
        await pilot.press("e")
        await pilot.pause()
        assert "noisy noisy noisy" in view.diff_text()
        await pilot.press("e")
        await pilot.pause()
        assert "noisy noisy noisy" not in view.diff_text()


@pytest.mark.asyncio
async def test_selection_change_resets_scroll(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=1.0)
    long_body = "diff --git a/big.py b/big.py\n+++ b/big.py\n" + "".join(
        f"+line {i}\n" for i in range(200)
    )
    _seed(view, {
        wa: [
            _fd("big.py", long_body, additions=200),
            _fd("small.py", "diff --git a/small.py b/small.py\n+++ b/small.py\n+one\n"),
        ],
    })
    async with view.run_test() as pilot:
        await pilot.pause()
        # big.py is selected first; scroll the diff pane down
        view.scroll_diff_to(20)
        assert view.diff_scroll_y() > 0
        view.select_file(wa, "small.py")
        await pilot.pause()
        assert view.diff_scroll_y() == 0


@pytest.mark.asyncio
async def test_refresh_preserves_scroll_when_selection_unchanged(tmp_path):
    """A watch-tick refresh with the same selection must not yank scroll to top."""
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=1.0)
    long_body = "diff --git a/big.py b/big.py\n+++ b/big.py\n" + "".join(
        f"+line {i}\n" for i in range(200)
    )
    _seed(view, {wa: [_fd("big.py", long_body, additions=200)]})
    async with view.run_test() as pilot:
        await pilot.pause()
        view.scroll_diff_to(30)
        y_before = view.diff_scroll_y()
        assert y_before > 0
        # Simulate a watch-tick refresh (same selection, same content)
        view._refresh_content()
        await pilot.pause()
        assert view.diff_scroll_y() == y_before, (
            f"scroll yanked on refresh: was {y_before}, now {view.diff_scroll_y()}"
        )


@pytest.mark.asyncio
async def test_refresh_preserves_selection_when_possible(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=0.05)
    _seed(view, {
        wa: [
            _fd("a.py", "diff --git a/a.py b/a.py\n+++ b/a.py\n+one\n"),
            _fd("b.py", "diff --git a/b.py b/b.py\n+++ b/b.py\n+two\n"),
        ],
    })
    async with view.run_test() as pilot:
        await pilot.pause()
        view.select_file(wa, "b.py")
        await pilot.pause()
        assert "two" in view.diff_text()
        # Same files present on refresh — selection preserved.
        view._refresh_content()
        await pilot.pause()
        assert "two" in view.diff_text()
        # b.py removed — falls back to first available.
        _seed(view, {wa: [_fd("a.py", "diff --git a/a.py b/a.py\n+++ b/a.py\n+one\n")]})
        view._refresh_content()
        await pilot.pause()
        assert "one" in view.diff_text()


@pytest.mark.asyncio
async def test_empty_tree_shows_no_changes(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=1.0)
    _seed(view, {wa: []})
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No changes" in view.diff_text()
