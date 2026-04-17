# CLI Reference

All task-scoped commands (`status`, `phase`, `test`, `journal`, `view …`, etc.) resolve their target task in this priority order:

1. `--task <slug>` flag — explicit, highest priority.
2. `MSHIP_TASK` env var — scope a whole shell session to one task.
3. cwd — if your shell is inside a task's worktree, that task is the default.

With 0 active tasks the command errors with "no active task". With exactly 1 active task and no anchor, the command targets that task. With 2+ active tasks and no anchor you'll get an "Ambiguous" error listing the active slugs — fix by anchoring via any of the three mechanisms above.

## Lifecycle

```bash
mship init [--detect | --name N --repo PATH:TYPE]   # scaffold mothership.yaml
mship init --install-hooks                          # (re)install pre-commit guard on every git root
mship spawn "description" [--repos a,b] [--skip-setup] [--bypass-reconcile]
mship switch <repo>                                 # cross-repo context switch
mship phase plan|dev|review|run [-f]                # transition with soft-gate warnings
mship block "reason" | mship unblock
mship test [--all] [--repos|--tag] [--no-diff]
mship journal [-]                                   # read task log; pass message to append
mship journal "msg" [--action X] [--open Y] [--repo R] [--test-state pass|fail|mixed]
mship journal --show-open                           # list open questions
mship finish [--body-file PATH | --body TEXT] [--base B] [--base-map a=B,b=B] [--push-only] [--handoff] [--force-audit] [--bypass-reconcile] [--force]
mship close [--yes] [--abandon] [--force] [--skip-pr-check] [--bypass-reconcile]
```

## Inspection

```bash
mship status                                        # task, phase, branch, drift, last log, finished warning
mship context                                       # one-shot agent-readable JSON snapshot of workspace state
mship dispatch --task <slug> -i "<instruction>"     # emit self-contained subagent prompt to stdout
mship audit [--repos r] [--json]
mship reconcile [--json] [--ignore SLUG] [--clear-ignores] [--refresh]
mship view status|logs|diff|spec [--watch]
mship view spec --web                               # serve rendered spec on localhost
mship graph
mship worktrees
mship doctor
```

## Maintenance

```bash
mship sync [--repos r]                              # fast-forward behind-only clean repos
mship prune [--force]                               # remove orphaned worktrees
```

## Long-running services

```bash
mship run [--repos a,b] [--tag t]                   # start services per dependency tier
mship logs <service>                                # tail logs for a service
```

## `mship finish`

### PR body

`mship finish` rejects empty PR bodies. Two ways to provide one:

```bash
mship finish --body-file /tmp/pr-body.md            # read from file
echo "..." | mship finish --body-file -             # read from stdin
mship finish --body "inline text"                   # inline (also supports `-` for stdin)
```

A TTY guard on both `-` forms errors fast if stdin is an interactive terminal instead of hanging.

### PR base branch

Each repo's PR can target a non-default base. Resolution order (most-specific wins):

- `--base <branch>` — global override for all repos.
- `--base-map cli=main,api=release/x` — per-repo overrides.
- `base_branch` in the repo's `mothership.yaml` entry.
- Remote default branch.

`mship finish` verifies every resolved base exists on `origin` before any push.

### `--force` vs normal re-finish

`mship finish` is idempotent: a second run after `finished_at` is stamped is a no-op. To push additional commits to the existing PRs (e.g., reviewer feedback), use `mship finish --force`. It pushes, updates `finished_at`, writes a `re-finished` journal entry, and does NOT create a new PR or modify the existing body. Edit the body separately via `gh pr edit <url> --body-file <path>`.

## Drift audit & sync

### Issue codes

- Errors (block `spawn`/`finish`): `path_missing`, `not_a_git_repo`, `fetch_failed`, `detached_head`, `unexpected_branch`, `dirty_worktree`, `no_upstream`, `behind_remote`, `diverged`, `extra_worktrees`.
- Warnings (don't block): `dirty_untracked` (untracked files only).
- Info-only: `ahead_remote`.

### Per-repo policy

```yaml
repos:
  schemas:
    path: ../schemas
    expected_branch: marshal-refactor
    allow_dirty: false
    allow_extra_worktrees: false
```

### Workspace policy

```yaml
audit:
  block_spawn: true
  block_finish: true
```

### Commands

- `mship audit [--repos r1,r2] [--json]` — exit 1 on any error-severity drift.
- `mship sync [--repos r1,r2]` — fast-forwards behind-only clean repos.
- `mship spawn --force-audit` / `mship finish --force-audit` — bypass with a line logged to the task log.

## Live views

`mship view` provides read-only TUIs designed for tmux/zellij panes. All views support `--watch` and `--interval N`.

- `mship view status [--task <slug>] [--watch]` — all tasks stacked by default; `--task` narrows to one.
- `mship view logs [--task <slug>] [--watch]` — tail the task's log.
- `mship view diff [--task <slug>] [--watch]` — per-worktree git diff.
- `mship view spec [name-or-path] [--task <slug>] [--watch] [--web]` — cross-task spec index picker by default.

Keys: `q` quit, `j/k` or arrows to scroll, `PgUp/PgDn`, `Home/End`, `r` force refresh.
