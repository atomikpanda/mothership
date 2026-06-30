"""Tests for active-task-aware enrichment of dirty-main-checkout audit issues.

These exercise the pure `_enrich_active_task` helper directly — no shell, no
git. The helper appends a "edit in its worktree" hint to a `dirty_worktree`
issue when the repo has an active task, and leaves everything else untouched.
"""
from __future__ import annotations

from mship.core.repo_state import Issue, _enrich_active_task


def test_enriches_dirty_worktree_when_active():
    issues = (Issue("dirty_worktree", "error", "3 modified tracked files"),)
    out = _enrich_active_task(issues, True)
    assert out[0].code == "dirty_worktree"
    assert out[0].severity == "error"
    # Original substring preserved (regression-critical: gates/tests key off it).
    assert "3 modified tracked files" in out[0].message
    # Hint mentions both the task and the worktree.
    assert "task" in out[0].message.lower()
    assert "worktree" in out[0].message.lower()


def test_no_change_without_active_task():
    issues = (Issue("dirty_worktree", "error", "3 modified tracked files"),)
    assert _enrich_active_task(issues, False) == issues


def test_untracked_not_enriched_even_when_active():
    issues = (Issue("dirty_untracked", "warn", "2 untracked files"),)
    # Only dirty_worktree is enriched; an untracked-only warn is left alone.
    assert _enrich_active_task(issues, True) == issues


def test_mixed_issues_only_dirty_worktree_changed_order_preserved():
    issues = (
        Issue("dirty_worktree", "error", "3 modified tracked files"),
        Issue("dirty_untracked", "warn", "2 untracked files"),
    )
    out = _enrich_active_task(issues, True)
    assert len(out) == 2
    # Order preserved.
    assert out[0].code == "dirty_worktree"
    assert out[1].code == "dirty_untracked"
    # Only the dirty_worktree message changed.
    assert "3 modified tracked files" in out[0].message
    assert "task" in out[0].message.lower()
    assert "worktree" in out[0].message.lower()
    # The untracked issue is unchanged, identity-equal to the input.
    assert out[1] == issues[1]
