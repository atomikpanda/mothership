# `mship view diff` — re-resolve worktree paths on every refresh

**Status:** Design approved, ready for implementation.
**Date:** 2026-04-15

## Purpose

`mship view diff` resolves worktree paths once at command launch. When a user launches the zellij layout (which starts `mship view diff --watch` in a pane before any task exists), then later runs `mship spawn` or `mship switch`, the diff view keeps pointing at the old paths — it shows the main checkout, not the task's worktree. Fix: re-resolve paths on every refresh tick.

## Change

`DiffView` gains an optional `resolve_paths: Callable[[], tuple[list[Path], Path | None]] | None` parameter. When set, `_refresh_content` invokes it first to compute `(all_paths, scope_path)` for this tick, then updates `self._paths` using the existing scope-filter rule.

When unset (tests that inject static paths), current behavior is preserved — `self._paths` and `self._scope_to_active_path` are captured at `__init__` and never change.

`register()` passes a closure:

```python
def _resolver():
    all_paths = _collect_workspace_worktrees(container)
    scope = None
    if not all_:
        state = container.state_manager().load()
        if state.current_task is not None:
            task = state.tasks[state.current_task]
            if task.active_repo is not None and task.active_repo in task.worktrees:
                scope = Path(task.worktrees[task.active_repo])
    return all_paths, scope
```

## Behavior

- **First refresh after a task is spawned** — resolver returns the new worktree paths, `self._paths` updates, tree rebuilds with the new worktrees, first file selected. No restart required.
- **Task closed** — resolver returns config repo paths, tree rebuilds.
- **`active_repo` changes via `mship switch`** — scope narrows to the new worktree; other worktrees drop out of the tree.
- **Paths unchanged across refresh** — `self._paths` stays equal; existing selection-preservation logic (already in `_rebuild_tree`) keeps the user's cursor.

## Non-Goals

- Signal-based refresh (inotify on `state.yaml`). The existing `--interval` cadence is fine.
- Live tree diffing to avoid a full rebuild when paths are stable. `_rebuild_tree` already preserves selection; a full rebuild per tick is cheap.
- Changing `register()`'s CLI surface. `--all` still works as today.

## Files Touched

- `src/mship/cli/view/diff.py` — add `resolve_paths` parameter; call it in `_refresh_content`; update `register()` to pass the closure instead of pre-resolving.
- `tests/cli/view/test_diff_view.py` — new test asserting the resolver drives path updates across refreshes.

## Testing

One new test: construct a `DiffView` with a resolver closure that returns different paths on successive calls. After first mount, assert tree reflects the first resolver return. Trigger a refresh; assert tree now reflects the second. Seed both states via `_test_override` so no real git is involved.

Existing tests unchanged (they pass `worktree_paths` directly, `resolve_paths=None`).

## Out of Scope

- `view spec` becoming task-aware (separate feature; deferred per user scoping).
