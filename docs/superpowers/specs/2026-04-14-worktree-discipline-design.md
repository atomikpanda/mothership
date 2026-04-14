# Worktree Discipline: View Scoping + cwd Warnings + Close Safety

**Status:** Design approved, ready for implementation.
**Date:** 2026-04-14

## Purpose

Three real pain points from dogfooding, bundled because they share a theme: **mship knows which worktree the agent should be in, and currently doesn't surface or enforce that strongly enough.**

1. **View commands don't scope to the active repo.** `mship view diff` shows every worktree even when `active_repo` is set, forcing the agent to hunt.
2. **Agents edit the main repo instead of the worktree.** `mship switch` shows the worktree path, but agents skim past it.
3. **`mship close --yes` before `mship finish`** destroys work. Current `close` treats "no PRs" as "cancelled before finish" and cleans up silently; unmerged, unpushed commits vanish.

## Changes

### #1 View scoping to `active_repo`

- **`mship view diff`**: when `task.active_repo` is set, the tree defaults to showing only that worktree's changes. `--all` opts out.
- **`mship view logs`**: when `task.active_repo` is set, default-filter entries to `e.repo == active_repo`. `--all` shows all entries.
- **`mship view status`**: no change — already task-level.

Implementation: add `scope_to_active: bool` parameter (default True) to the relevant view constructors, threaded through `gather()` or `_rebuild_tree()`. `--all` flag sets it False.

### #2 Worktree discipline

**`mship switch <repo>` output:**
The handoff's first rendered line becomes an unmissable `cd` hint:

```
⚠ cd /path/to/.worktrees/feat/add-labels

Switched to: cli (task: add-labels, phase: dev)
...
```

TTY only (non-TTY JSON unchanged). Uses red/bold. Omit the line if the agent is already inside the worktree (`Path.cwd()` inside `worktree_path`).

**`mship log` + `mship test` cwd warning:**
After loading state, if `task.active_repo` is set, check whether `Path.cwd().resolve()` is inside `task.worktrees[active_repo].resolve()`. If not, print a yellow warning to stderr and continue:

```
⚠ running from /elsewhere, not the active repo's worktree at /path/to/wt
  (commands still run in the correct path, but edits in your shell won't affect the worktree)
```

Non-blocking — some legit cases (agents running mship from elsewhere). `--force` not needed; the warning is informational.

**Skill doc:** add to the "During work" section:
```
After `mship switch <repo>`, `cd` to the worktree shown at the top of the handoff.
mship will warn if you forget, but your edits still go to the wrong place.
```

### #3 `close` lifecycle + recovery-path check

Revised flag model for `mship close`:

| Flag | Effect |
|---|---|
| (none) | Refuses if `finished_at is None`. Still runs recovery-path check. |
| `--abandon` | Bypasses the `finished_at is None` refusal. Still runs recovery-path check. |
| `--force` | Bypasses every safety check (finish-required AND recovery-path). Truly destructive. |
| `--skip-pr-check` | Skip `gh pr view` calls. Existing; unchanged. |
| `--yes` | Skip confirmation prompt. Existing; unchanged. |

**Finish-required check:**

If `task.finished_at is None` and `--abandon` not passed:
```
Cannot close: task hasn't been finished.
Run `mship finish` to create PRs, or `mship close --abandon` to discard without PRs.
```
Exit 1.

**Recovery-path check:**

Runs whenever `close` would destroy worktrees. For each affected repo, compute:

1. Does the worktree exist at `task.worktrees[repo]`? If not, skip (nothing to destroy there).
2. Does the task branch have commits past its base? `git rev-list --count <base>..<branch>` in the worktree. If zero, skip (no work to lose).
3. Is the branch merged into base locally? `git merge-base --is-ancestor <branch> <base>`. If True, recoverable.
4. Does `task.pr_urls[repo]` exist? If True, PR preserves history. Recoverable.
5. Is the branch pushed to origin at the same SHA? `git ls-remote origin <branch>` returns a SHA matching local HEAD. Recoverable.

If all affected repos pass at least one recovery check, proceed.

If any repo fails all recovery checks, refuse unless `--force`:

```
Cannot close: unrecoverable commits in these repos:
  cli: feat/add-labels (3 commits, not merged to main, not pushed, no PR)
  api: feat/add-labels (1 commit, not merged to cli-refactor, not pushed, no PR)

These will be permanently lost. Options:
  - `mship finish` to create PRs
  - push from each worktree to save work
  - `mship close --force` to delete anyway (destructive)
```
Exit 1.

**Base branch resolution** for the checks: same as `mship finish` — via `resolve_base()` with no CLI overrides. Missing base → skip that repo's recovery check conservatively (treat as recoverable — we can't prove it's not).

## Non-Goals

- Blocking `mship log`/`test` on wrong cwd. Warning only.
- `mship switch --cd` emitting shell eval lines. Deferred.
- Auto-cd via terminal hints (OSC-7). Too fragile.
- Parallelizing the per-repo recovery checks. Typical workspace is small; sequential is fine.
- Fetching before the push check. `git ls-remote` hits origin directly — no local cache to stale.

## Files Touched

- `src/mship/cli/view/diff.py` — add `scope_to_active` / `--all`; filter `self._paths` to the active worktree when scoping.
- `src/mship/cli/view/logs.py` — add `--all`; filter log entries by repo when scoping.
- `src/mship/cli/switch.py` — prepend the `⚠ cd` line to TTY handoff output when cwd differs from worktree_path.
- `src/mship/cli/log.py` + `src/mship/cli/exec.py` (test_cmd) — add cwd-vs-active-worktree check after state load.
- `src/mship/cli/worktree.py` (close command) — `--abandon` flag; finish-required check; recovery-path check.
- `src/mship/core/pr.py` — reuse `count_commits_ahead`; new `check_merged_into_base(repo_path, branch, base) -> bool` and `check_pushed_to_origin(repo_path, branch) -> bool`.
- `skills/working-with-mothership/SKILL.md` — add the post-switch `cd` guidance.
- `tests/cli/view/test_diff_view.py`, `tests/cli/view/test_logs_view.py` — scoping tests.
- `tests/cli/test_switch.py` — `cd` line appears/omits based on cwd.
- `tests/cli/test_log.py`, `tests/test_test_diff_integration.py` — cwd warning fires when outside active worktree, silent when inside.
- `tests/cli/test_worktree.py` — close flag matrix: no-flags + unfinished refuses; `--abandon` proceeds when recoverable; unrecoverable refuses; `--force` proceeds anyway.
- `tests/core/test_pr.py` — `check_merged_into_base`, `check_pushed_to_origin`.

## Testing

Each check needs one passing test and one failing test. Recovery-path check gets a matrix: merged-only, pushed-only, pr-only, none → none refuses without `--force`.

`--force` tests assert the operation proceeds AND the task log entry records it as forced.
