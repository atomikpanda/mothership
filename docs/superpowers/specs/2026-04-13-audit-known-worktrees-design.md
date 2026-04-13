# `extra_worktrees` — exclude mship-created worktrees

**Status:** Design approved, ready for implementation plan.
**Date:** 2026-04-13

## Purpose

`mship audit`'s `extra_worktrees` check fires any time more than one git worktree exists at a repo's git root. mship's own workflow creates worktrees — after any `mship spawn`, this count is ≥ 2 by construction. Result: `mship audit` false-positives constantly, and `mship finish` cannot proceed under the default `block_finish: true` gate, because auditing the finish-bound repo sees its own task worktree as "extra."

This spec redefines the check: `extra_worktrees` means "a worktree exists at a path mship doesn't know about." Known worktrees — those registered in `state.tasks[*].worktrees` — are excluded before counting.

Orphaned worktrees (directories that exist on disk but whose task was removed from state) are intentionally **not** excluded: they *are* drift mship doesn't track, and `mship prune` already handles them. The error message adds a hint pointing at `mship prune`.

## Non-Goals

- Distinguishing orphans from foreign worktrees with a new issue code. They fall under the same umbrella.
- Auto-pruning orphans during audit. `mship prune` stays a separate, explicit command.
- Flagging mship-created worktrees for tasks that are marked complete but not cleaned up (that's a `prune` concern, not an audit concern).

## Behavior Change

### Before
```
extra_worktrees (error): 2 worktrees exist; expected 1
```
Fires on any workspace that has spawned a task.

### After
```
extra_worktrees (error): 1 worktree at a path mship doesn't track (run `mship prune` to list/clean orphans, or check for foreign worktrees)
```
Only fires for worktrees whose resolved path is not in state's union of known worktree paths. Every entry in `state.tasks[*].worktrees` is considered known, across **all** tasks (not just the current one).

## Architecture

### New helper

`src/mship/core/view/diff_sources.py` is unrelated. The change lives entirely in `src/mship/core/repo_state.py`.

Factor out:
```python
def _list_worktree_paths(shell: ShellRunner, root_path: Path) -> list[Path]:
    """Parse `git worktree list --porcelain` into absolute, resolved Paths."""
```

### Signature change

```python
def audit_repos(
    config: WorkspaceConfig,
    shell: ShellRunner,
    names: Iterable[str] | None = None,
    known_worktree_paths: frozenset[Path] = frozenset(),
) -> AuditReport:
```

Default empty set → current behavior (used only when state is unreachable from the caller). `_probe_git_wide` accepts the same set and filters before counting.

### Filter logic

Inside `_probe_git_wide`, after collecting the worktree paths:

```python
wt_paths = _list_worktree_paths(shell, root_path)
unknown = [p for p in wt_paths if p not in known_worktree_paths]
if not allow_extra_worktrees and len(unknown) > 1:
    issues.append(Issue(
        "extra_worktrees", "error",
        f"{len(unknown) - 1} worktree(s) at paths mship doesn't track "
        "(run `mship prune` to list/clean orphans, or check for foreign worktrees)",
    ))
```

The threshold stays at 1 because the main checkout itself counts — `git worktree list` always includes it. So `len(unknown) > 1` means: "the main checkout plus at least one path mship doesn't track."

### Path normalization

`git worktree list --porcelain` emits absolute paths; state entries are stored as whatever the caller passed in. Both sides go through `Path(p).resolve()` before the set-membership check. This handles `..`, symlinks, and trailing-slash differences.

## Call-Site Wiring

Every call site that has access to `state` builds the known set and passes it:

- `src/mship/cli/audit.py` — `mship audit` command.
- `src/mship/cli/sync.py` — `mship sync` command (via its own `audit_repos` call).
- `src/mship/cli/worktree.py` — spawn gate and finish gate.
- Any future caller that operates at the CLI layer.

Shared helper in `src/mship/core/audit_gate.py`:

```python
def collect_known_worktree_paths(state_manager) -> frozenset[Path]:
    """Resolved union of every worktree path across every task in state."""
    state = state_manager.load()
    paths: set[Path] = set()
    for task in state.tasks.values():
        for raw in task.worktrees.values():
            paths.add(Path(raw).resolve())
    return frozenset(paths)
```

Callers that don't already load state can use this without adding state-loading logic inline.

## Testing

### Unit tests (`tests/core/test_repo_state.py`)

- `_list_worktree_paths` parses standard porcelain output into resolved absolute paths.
- `_list_worktree_paths` tolerates empty output (returns `[]`).
- Audit of a repo with one extra worktree at a path in `known_worktree_paths` → no `extra_worktrees` issue.
- Audit of a repo with one extra worktree at a path **not** in `known_worktree_paths` → `extra_worktrees` fires; message contains `mship prune`.
- Audit of a repo with two extra worktrees — one known, one foreign → `extra_worktrees` fires; counts correctly reflect the foreign one.

### Integration tests

- `tests/test_finish_integration.py`: `mship finish` on a task with its own worktree no longer blocks on `extra_worktrees` under default `block_finish: true`.
- `tests/cli/test_audit.py`: `mship audit` in a workspace with a spawned task reports clean (no `extra_worktrees`).
- `tests/cli/test_audit.py`: `mship audit` with an active task **plus** a foreign worktree path still fires `extra_worktrees`.

## Error Handling

- State file missing or corrupt → `collect_known_worktree_paths` propagates the loader's existing error (which the CLI already handles). Callers in `audit`/`sync`/spawn/finish already do `container.state_manager().load()` — same path.
- `Path(p).resolve()` on a path that no longer exists still returns an absolute path (doesn't raise); comparison against `git worktree list` output works regardless. No special-casing needed.
- Worktrees for completed-but-not-pruned tasks: treated as known (still in state). `mship abort --yes` removes them from state, at which point they become orphans and fire the check — which is the intended behavior.

## Out of Scope (post-v1)

- Separate `orphan_worktree` issue code.
- Auto-pruning orphans during audit.
- Config knob to disable the check entirely (use `allow_extra_worktrees: true` — already exists).
