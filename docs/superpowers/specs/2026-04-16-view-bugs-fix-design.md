# View bugs — fixes for #17, #18, and status-refresh-post-close

**Status:** Approved
**Date:** 2026-04-16

## Bug A — `mship view spec --watch` (#17)

1. **Scroll jumps on refresh.** Cause: `SpecIndexApp._refresh_index` calls `DataTable.clear()` then re-adds every row, which resets cursor and scroll. Fix: track the table's `scroll_y` (and cursor row-key) before clear; restore after repopulate.
2. **Task list shows only current task.** The `state` is loaded once at app construction (`spec.py` passes `state=container.state_manager().load()`). A `state_loader` was added for the index refresh, but the *initial* `compose()` uses the snapshot. First render therefore reflects state at launch. Fix: call `state_loader()` in `compose()` too if available, falling back to `state`.
3. **Files not sorted newest-first globally.** `find_all_specs` groups by task (active before finished) and sorts within each group by mtime desc, producing a non-flat global order. Fix: sort the final list flat by mtime desc; `task_slug` remains a column, not a grouping key.

## Bug B — `mship view diff --watch` (#18)

Current `collect_worktree_diff(worktree_path)` only shows `git diff` (working tree vs index). Need the full task diff: commits on `task.branch` since the merge-base with `task.base_branch`, plus any uncommitted changes.

Fix: extend `collect_worktree_diff` (or add a sibling) to accept a `base_branch` argument and run `git diff <merge-base>...HEAD` plus append uncommitted working-tree changes. Wire through `view diff` so each worktree gets the correct base (`task.base_branch or "main"`).

## Bug C — `mship view status --watch` doesn't refresh after `mship close`

Hypothesis: `StateManager.load()` re-reads state.yaml on every call, so gather() should reflect changes. Need to confirm by test. If gather() produces correct post-close text, the bug is in the Textual refresh path or a terminal issue outside our control — document with a test anyway.

Fix approach: add a unit test that drives `StatusView.gather()` before/after state mutation, asserting the output changes. If test fails, we know where to fix. If test passes, issue is upstream of our code.

## Implementation

Single task, one PR. All three fixes with tests.
