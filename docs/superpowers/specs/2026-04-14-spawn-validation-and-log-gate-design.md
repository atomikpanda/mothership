# Spawn `git_root` Validation + `mship log` cwd Hard-Error

**Status:** Design approved, ready for implementation.
**Date:** 2026-04-14

## Purpose

Two tight guardrails driven by observed failure modes:

1. **`mship spawn --repos web` when `web.git_root == tailrd`** — silently produces a broken task where `web`'s worktree can't exist in isolation (the shared git root means both repos share one worktree). Agent ends up editing the main checkout. Must fail at spawn time with a clear remediation.

2. **`mship log` from outside the active worktree** — currently prints a yellow warning and logs anyway, which means the wrong paper trail. Escalate to a hard error by default; `--force` bypasses and logs a `bypassed: cwd outside worktree` entry.

## Changes

### #1 Spawn validates `git_root` dependencies

At the top of `mship spawn`, after `affected_repos` is resolved but before any worktree creation:

For each repo `r` in `affected_repos`:
- Let `root = config.repos[r].git_root`.
- If `root is None`: OK.
- If `root in affected_repos`: OK.
- Else: refuse with:
  ```
  Cannot spawn: 'web' shares git_root with 'tailrd', which is not in this task.
  Worktree isolation will not work because they share one git checkout.
  Add tailrd to --repos or spawn with --repos tailrd,web.
  ```
  Exit 1. Zero state changes.

Multiple violations → list all of them in one error block before exiting.

### #2 `mship log` hard-errors when cwd outside active worktree

Change the cwd warning in `mship log` (shipped earlier this session) from warning to error **by default**.

- Default: refuse. Exit 1 with the existing warning text + an extra line "Run from the worktree, or `mship log --force "msg"` to override."
- `--force`: bypasses the check, writes the log entry, and prepends an `action=cwd-bypass` tag to the entry so the bypass is discoverable later.
- Only fires when `task.active_repo` is set. Tasks with no active repo (agent hasn't `switch`ed yet) still write freely.

`mship test` stays warning-only (unchanged) — tests execute via the executor which uses the correct cwd regardless; the warning there is advisory about where *edits* land, not where tests run.

## Non-Goals

- Reworking `switch` to refuse until cwd matches. Out of scope.
- Rogue-commit detection in audit. Deferred.
- Pre-commit hook installation. Separate spec.
- Retroactive migration for existing tasks with git_root issues already spawned — the validation only fires at spawn time.

## Files Touched

- `src/mship/cli/worktree.py` (spawn command) — add the `git_root` validation check before calling `wt_mgr.spawn`.
- `src/mship/cli/log.py` — change cwd-mismatch handling: error by default, `--force` flag bypasses and tags the entry.
- `tests/cli/test_worktree.py` — spawn validation tests (passes when git_root present in --repos; fails when absent; multiple violations listed).
- `tests/cli/test_log.py` — update existing cwd tests; add `--force` bypass test and the `action=cwd-bypass` assertion.

## Testing

**Spawn validation:**
- spawn with `--repos web` where `web.git_root=tailrd` and tailrd NOT in --repos → exit 1, error contains "web", "tailrd", and "--repos".
- spawn with `--repos tailrd,web` (both present) → succeeds.
- spawn with two violations → both mentioned in the error.
- Regression: spawn for a repo with no `git_root` field still works.

**Log gate:**
- log from outside active worktree → exit 1, error message mentions the worktree path.
- `log --force "msg"` from outside → exit 0, entry written with `action=cwd-bypass`.
- log from *inside* active worktree → exit 0, no warning, entry written normally.
- log when `active_repo` is None → no cwd check; entry writes normally.
