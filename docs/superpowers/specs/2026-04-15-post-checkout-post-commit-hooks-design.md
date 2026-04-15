# `post-checkout` + `post-commit` hooks

**Status:** Design approved, ready for implementation.
**Date:** 2026-04-15

## Purpose

The existing pre-commit hook catches the worst failure (commit in the wrong place), but it fires late — the agent has already wasted time editing in main. Add two more hooks:

- **`post-checkout`** — warns immediately when the agent creates/switches to a branch outside `mship spawn`. Early signal.
- **`post-commit`** — auto-logs every commit to the active task's mship log. Catches the "agent doesn't `mship log`" pattern structurally.

Neither hook *blocks* (post-checkout can't; post-commit has nothing to gate). Both make skipping mship either loudly visible (post-checkout) or structurally redundant (post-commit auto-records so the agent doesn't need to remember).

## Changes

### 1. `post-checkout` hook

Installed at `<git_root>/.git/hooks/post-checkout` alongside the existing pre-commit hook. Same MSHIP-BEGIN/END marker pattern.

Hook body (~8 lines of POSIX):
```sh
# MSHIP-BEGIN — managed by mship
if command -v mship >/dev/null 2>&1; then
    prev_head="$1"
    new_head="$2"
    is_branch_checkout="$3"
    [ "$is_branch_checkout" = "1" ] && mship _post-checkout "$prev_head" "$new_head" || true
fi
# MSHIP-END
```

`mship _post-checkout <prev> <new>` logic:

1. Load state. If no `mothership.yaml` or fail-open error → exit 0.
2. Read current branch: `git rev-parse --abbrev-ref HEAD`.
3. If branch is in `{main, master, develop}` → exit 0 (legitimate to check these out).
4. If no active task → print warning to stderr:
   ```
   ⚠ mship: checked out 'feat/foo' but no active mship task. If you're starting feature
     work, run `mship spawn "<description>"` — it'll give you a proper worktree and state.
   ```
   Exit 0 (informational only).
5. If active task exists and `branch == task.branch` AND cwd is inside one of `task.worktrees[*]` → exit 0 (correct flow).
6. If active task exists and `branch != task.branch` → print warning:
   ```
   ⚠ mship: checked out 'feat/foo' but active task 'add-labels' is on 'feat/add-labels'.
     If this was a mistake, `git checkout feat/add-labels` in the worktree. If you're
     switching tasks entirely, run `mship close --abandon` first.
   ```
   Exit 0.
7. If active task exists and `branch == task.branch` but cwd is NOT a task worktree (agent checked out the feature branch in the main checkout) → print warning:
   ```
   ⚠ mship: you checked out 'feat/add-labels' here, but the task's worktree is
     /abs/.worktrees/feat/add-labels. cd there — don't edit in main.
   ```
   Exit 0.

All warnings go to stderr so they don't pollute `git` output capture.

### 2. `post-commit` hook

Installed at `<git_root>/.git/hooks/post-commit`.

Hook body (~5 lines):
```sh
# MSHIP-BEGIN — managed by mship
if command -v mship >/dev/null 2>&1; then
    mship _log-commit || true
fi
# MSHIP-END
```

`mship _log-commit` logic:

1. Load state. No active task or no mothership.yaml → exit 0 silently.
2. Get current cwd via `Path.cwd().resolve()`.
3. Walk `task.worktrees`: find the repo name whose resolved path equals cwd. None → exit 0 (commit happened outside any task worktree; pre-commit hook should have blocked this, but if `--no-verify` was used we respect the agent's override and don't log).
4. Read the new commit: `git log -1 --format=%H%n%s`.
5. Call log manager: append entry with `message = f"commit {sha[:10]}: {subject}"`, `action="committed"`, `repo=<detected>`, `iteration=task.test_iteration` if set.
6. Exit 0.

`--no-verify` bypasses pre-commit and therefore also means the agent consciously skipped mship's commit check. Respect that: don't auto-log those either (step 3 covers this implicitly — `--no-verify` commits in main won't match any worktree path).

### 3. Install all three hooks

Extend `core/hooks.py`:

- Rename `install_hook(git_root)` → install all three (pre-commit, post-checkout, post-commit) at `<git_root>/.git/hooks/`.
- Each hook file uses the same MSHIP-BEGIN/END marker pattern; each has its own template.
- `is_installed(git_root)` → all three must be present with marker for it to return True.
- `uninstall_hook(git_root)` → strips the MSHIP block from all three files.

Alternative implementation to keep the API surface small: three separate functions `install_pre_commit`, `install_post_checkout`, `install_post_commit`, and `install_hook` calls all three.

### 4. Doctor check

Doctor's existing `hooks/<root.name>` check becomes three checks (one per hook type) or remains a single check that fails when any of the three are missing. Recommend single check for simplicity: "pre-commit/post-checkout/post-commit hooks installed" — passes when all three have the MSHIP marker.

### 5. `mship init --install-hooks`

Already installs the pre-commit hook. Extend to install all three on every unique git root.

## Non-Goals

- Blocking bad checkouts. Post-checkout can't refuse; warnings are the best we can do here.
- Logging amend commits differently. Each amend fires post-commit; each gets its own log entry. Noise, accepted.
- Detecting the difference between `git checkout -b <new>` vs `git checkout <existing>`. We warn on both when branch doesn't match task expectations; the agent can tell which it was from context.
- Hooks that fire on `git switch` (newer git porcelain). `git switch` also triggers post-checkout, so this is automatically covered.
- Auto-logging tests, pulls, or other git operations. Only commits.

## Files Touched

- `src/mship/core/hooks.py` — templates for all three hooks; `install_hook` installs all three; `is_installed` checks all three; `uninstall_hook` strips all three.
- `src/mship/cli/internal.py` — new `_post-checkout` command (3 args: prev_head, new_head, is_branch); new `_log-commit` command (no args).
- `src/mship/core/doctor.py` — adjust the single hook check to verify all three present with marker.
- `src/mship/cli/init.py` — unchanged (already calls `install_hook`; just installs more now).
- Tests in `tests/core/test_hooks.py`, `tests/cli/test_check_commit.py` (rename or add sibling), `tests/test_hook_integration.py`.

## Testing

**Unit (`tests/core/test_hooks.py` extensions):**
- `install_hook` writes all three hook files with MSHIP markers.
- `is_installed` returns False when any hook is missing.
- `uninstall_hook` strips marker from all three.
- Idempotency preserved across all three.
- Existing pre-commit tests continue to pass (they check pre-commit specifically).

**CLI (`tests/cli/test_internal_hooks.py` new or extend existing):**
- `_post-checkout` with no active task + non-default branch → warns, exit 0.
- `_post-checkout` with active task + matching branch in worktree cwd → silent, exit 0.
- `_post-checkout` with active task + wrong branch → warns with task's branch in message.
- `_log-commit` in active task's worktree → appends log entry with action="committed".
- `_log-commit` outside any worktree (simulates `--no-verify` commit in main) → no log entry, exit 0.
- `_log-commit` with no active task → silent, exit 0.

**Integration (extends `tests/test_hook_integration.py`):**
- After `mship init --install-hooks`, all three hooks present.
- `git checkout -b new-branch` outside mship spawn → post-checkout emits warning.
- `git commit` inside worktree → auto-logs via `mship log`; verify via `mship log --last 1`.

## Out of Scope

- Pre-push hook (deferred).
- Blocking checkouts via a wrapper that intercepts `git` itself (not universal, user-hostile).
- Editor integrations.
