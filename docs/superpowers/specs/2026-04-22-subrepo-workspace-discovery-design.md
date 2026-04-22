# Subrepo Workspace Discovery — Design

Closes #84 and #86.

## Problem

Two coupled failures when mship is invoked from inside a subrepo worktree (where the worktree path is not an ancestor of `mothership.yaml`):

1. **#84 — direct command failure.** `mship <cmd>` from within e.g. `project_api/.worktrees/feat/my-task/` errors: `No mothership.yaml found in any parent directory`. The intended workflow is that real coding happens inside those worktrees; commands not working there fights the user.

2. **#86 — hook warning noise.** Git hooks (`pre-commit`, `post-checkout`, `post-commit`) installed by `mship init --install-hooks` invoke `mship _check-commit` / `_post-checkout` / `_journal-commit`. When the hook fires from a subrepo worktree, the same discovery failure prints `Error: No mothership.yaml found in any parent directory` on every commit. Commits succeed; warnings are noise that train users to ignore warnings.

Root cause is shared: `ConfigLoader.discover(start)` only walks parents of `start` looking for `mothership.yaml`. Subrepo worktrees live under a path that doesn't contain the workspace root.

## Solution

Per the task-hub direction memo (2026-04-21), v1 keeps per-repo worktree topology and adds a workspace-discovery pointer:

1. **`.mship-workspace` marker file** — dropped by spawn in every worktree, contains one line: the absolute path to the workspace root.

2. **`MSHIP_WORKSPACE` env var** — user override / escape hatch for cases the marker doesn't cover.

3. **`get_container(required=False)`** — hook commands treat missing workspace as a silent no-op instead of erroring.

## Scope

### In scope
- New `src/mship/core/workspace_marker.py` module: marker write + read helpers.
- `ConfigLoader.discover()` priority chain update.
- `get_container(required: bool = True)` parameter.
- Spawn drops the marker + updates per-worktree git exclude.
- Hook command implementations use `required=False`.

### Out of scope
- Workspace relocation detection / marker auto-refresh. Stale markers fall through gracefully; user re-spawns if paths change.
- Global `.gitignore` changes. The per-worktree exclude is git-native and doesn't leak.
- `--workspace <path>` CLI flag. Env var is sufficient; adding a flag per command is a maintenance burden for low marginal value.
- Non-mship git repos. The marker is spawned only by `mship spawn`; ad-hoc repos remain unchanged.

## Architecture

```
mship.core.workspace_marker
  ├─ MARKER_NAME = ".mship-workspace"
  ├─ write_marker(worktree_path, workspace_root) -> None
  ├─ read_marker(start: Path) -> Path | None
  └─ append_to_worktree_exclude(worktree_path, parent_git_dir) -> bool

mship.core.config.ConfigLoader.discover(start: Path) -> Path
  └─ priority: MSHIP_WORKSPACE env → marker walk-up → workspace walk-up → FileNotFoundError

mship.cli.__init__.get_container(required: bool = True) -> Container | None
  └─ required=True (default): error + exit on not-found (today's behavior)
  └─ required=False: return None on not-found

mship.core.worktree.WorktreeManager.spawn()
  └─ per worktree: write_marker() + append_to_worktree_exclude()
  └─ warning on exclude write failure; marker still written

mship.cli.{_check-commit, _post-checkout, _journal-commit} commands
  └─ get_container(required=False); return if None
```

## Marker details

**Filename:** `.mship-workspace` (leading dot — matches existing `.mothership/` directory convention for metadata that sits alongside tracked content).

**Contents:** one line — the absolute path of the dir containing `mothership.yaml`. No trailing newline required.

**Creation:** `WorktreeManager.spawn()` writes the marker after the worktree directory is created, before spawn returns.

**Per-worktree exclude:** git's `info/exclude` mechanism. Each git worktree has its own state dir at `<parent-repo>/.git/worktrees/<slug>/`. The file `<parent-repo>/.git/worktrees/<slug>/info/exclude` is honored only for that worktree — no cross-contamination. Spawn appends `.mship-workspace` to it (idempotently; don't duplicate if already present).

**Failure mode:** if the per-worktree exclude path doesn't exist or isn't writable (unusual — broken worktree state), spawn emits a warning and continues. The marker is still written; the user can manually add `.mship-workspace` to `.gitignore`.

**Stale marker handling:** if the marker points to a path that doesn't exist OR a dir without `mothership.yaml`, discovery treats it as absent and continues walking. No error, no warning — silent graceful degradation. The user will re-create the marker naturally on the next `mship spawn`.

## Discovery priority

`ConfigLoader.discover(start: Path) -> Path` becomes:

```
1. MSHIP_WORKSPACE env var:
   - not set → fall through
   - set and points to dir containing mothership.yaml → return that path/mothership.yaml
   - set but invalid → raise FileNotFoundError with explicit message naming the env var
     (misconfiguration should fail loudly, not silently walk up)

2. Marker walk-up from `start`:
   - iterate ancestors; if any has `.mship-workspace`, read it
   - marker contents is a path; if that path contains mothership.yaml, return it
   - if the path doesn't have mothership.yaml, continue walking (marker is stale)

3. Workspace walk-up from `start`:
   - existing behavior; iterate ancestors looking for `mothership.yaml`

4. Not found → raise FileNotFoundError (existing behavior)
```

## `get_container(required)` contract

**`required=True`** (today's behavior, the default): missing workspace → stderr error + `typer.Exit(1)`.

**`required=False`**: missing workspace → return `None`. Caller is responsible for handling `None` (typically: early return / no-op).

Only the hook command implementations should pass `required=False`. All user-facing commands keep the default. This preserves the loud-fail-when-user-expects-mship behavior while eliminating hook noise in non-mship repos.

## Hook command updates

Three hook-invoked commands in mship's CLI: `_check-commit`, `_post-checkout`, `_journal-commit`. Each one today starts with `container = get_container()`. Update each to:

```python
container = get_container(required=False)
if container is None:
    return  # silent no-op outside a workspace
# ... existing logic unchanged ...
```

The hooks already tolerate mship-not-on-PATH (`if command -v mship ...`); this extends the tolerance to "mship is installed but we're not in an mship workspace."

## Testing

### Unit — `tests/core/test_workspace_marker.py` (new)

- `write_marker(wt, root)` creates the file with the expected contents.
- `read_marker(start)` returns the path for an immediate match.
- `read_marker(start)` walks up ancestors to find the marker.
- `read_marker(start)` returns None when no marker is found.
- `read_marker(start)` returns None when marker points to a non-existent path (stale).
- `read_marker(start)` returns None when marker path exists but has no `mothership.yaml` (stale).
- `append_to_worktree_exclude(...)` appends `.mship-workspace` to the file.
- `append_to_worktree_exclude(...)` is idempotent — doesn't duplicate.
- `append_to_worktree_exclude(...)` returns False when the parent git dir path is unwritable / missing.

### Unit — `tests/core/test_config.py` additions

- `ConfigLoader.discover` honors `MSHIP_WORKSPACE` env var when set to a valid path.
- Invalid `MSHIP_WORKSPACE` → FileNotFoundError naming the env var.
- Marker walk-up takes precedence over workspace walk-up (marker points to A, walk-up would find B — A wins).
- Stale marker falls through to workspace walk-up.

### Integration — `tests/core/test_worktree.py` additions

- After spawn, each worktree contains `.mship-workspace` with the workspace root path.
- After spawn, each worktree's per-worktree exclude contains `.mship-workspace`.
- Invoking `mship status` from a subrepo worktree (simulated by adjusting cwd) finds the workspace via the marker.

### Integration — `tests/cli/test_hooks.py` (new or existing)

- `get_container(required=False)` returns None when cwd is outside any workspace.
- A hook command (`mship _check-commit <path>`) exits 0 silently when the path isn't under an mship workspace.
- A user command (`mship status`) still errors loudly in the same scenario.

## Migration / backward compatibility

- **Existing workspaces spawned before this PR** have no markers. Post-upgrade, `mship status` from a subrepo worktree still fails as before until the user re-spawns (or sets `MSHIP_WORKSPACE`). Acceptable — they can also `mship close` + re-spawn, or retrofit via `mship init --install-markers` (out of scope for v1; file as a followup if it becomes friction).
- **No state-file changes.** The marker is filesystem-only metadata; workspace state format is unchanged.

## Anti-goals

- No workspace-topology changes. Worktrees stay per-repo under `.worktrees/feat/...`.
- No auto-refresh or auto-cleanup of stale markers. Graceful fall-through only.
- No CLI flag for workspace override. Env var is enough.
- No marker for non-mship git repos. Only `mship spawn` writes markers.
