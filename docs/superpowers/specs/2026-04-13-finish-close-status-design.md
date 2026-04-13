# Task Lifecycle: `finish` → `close` + Status Enrichment

**Status:** Design approved, ready for implementation plan.
**Date:** 2026-04-13

## Purpose

Today's task lifecycle has two cliffs:

1. **Post-finish ambiguity.** `mship finish` creates PRs, but nothing in state says "you're done editing; await merge." Agents and humans keep working in the worktree, push new commits onto the already-open PR, and `mship status` still shows it as the current task. The only cleanup verb is `mship abort --yes` — semantically wrong for the happy path and easy to overlook.
2. **Thin `status` output.** `mship status` shows phase/branch/repos/tests/blocked but nothing about drift, time-in-phase, recent activity, or finish state. Users can't glance and know where they stand.

This spec resolves both:

- **Rename `abort` → `close`** with smart PR-state detection so it means "the task is resolved, tear down" whether the PR was merged, closed unmerged, or never existed.
- **`mship finish --push-only`** for non-PR workflows (merge queues, Graphite, direct-to-main).
- **`finished_at` state tracking** that puts the task into an "awaiting resolution" state, refusing backward phase transitions without `--force`.
- **Enriched `mship status`** with cheap local-only drift signals, time in phase, last log entry, and the finish nudge.

## Non-Goals

- Automated merge polling (`mship finish --wait-and-close`). User closes explicitly.
- Running `git fetch` during `mship status`. Cheap local checks only; full drift stays in `mship audit`.
- Removing `mship abort` via deprecation warnings. It's a hard rename (pre-1.0, OK to break).
- New workspace-level config. `--push-only` is per-invocation.
- Shell-prompt / cwd-detection integration.

## Vocabulary

Before → after:

| Action | Before | After |
|---|---|---|
| Implementation done, push + PR | `mship finish` | `mship finish` (unchanged) |
| Implementation done, push only (non-PR workflow) | n/a | `mship finish --push-only` |
| Task resolved, clean up | `mship abort --yes` | `mship close` (auto-detects state) |
| Cancel a task before finishing | `mship abort --yes` | `mship close` (logs as cancelled) |

`close` replaces `abort` everywhere. The word "abort" leaves the vocabulary.

## Data Model

### `Task` additions

```python
class Task(BaseModel):
    ...existing fields...
    finished_at: datetime | None = None
    phase_entered_at: datetime | None = None
```

- `finished_at` — set by `mship finish` on success. None means the task is still in an editable state.
- `phase_entered_at` — set by `mship phase` on any transition. Used for `status`'s "phase: dev for 3h" line.

Existing state files with neither field load with `None` defaults (Pydantic default). No migration script; pre-1.0 accepts the gap.

### `TaskLog` unchanged

Log entries already carry timestamps. Latest entry is read via existing `log_manager.read(slug, last=1)`.

## `mship finish`

### Behavior

Runs the existing flow (audit gate → resolve bases → verify bases → verify commits-ahead → push → create PRs → stamp pr_urls) **plus**:

- On successful completion: sets `task.finished_at = datetime.now(UTC)`.
- Prints a final nudge line: *"Task finished. After merge, run `mship close` to clean up."*
- Idempotent re-runs (when all repos already have pr_urls) also stamp `finished_at` if not already set — in case an earlier run stamped PR URLs but crashed before state flush.

### `--push-only` flag

New option: `mship finish --push-only`.

- Runs: audit gate → verify each branch has commits ahead of its base (same pre-flight as today) → `git push -u origin <branch>` per repo.
- Skips: `gh auth status` check, base-resolution step (there's no PR so no base), `gh pr create`, `pr_urls` state writes.
- Still stamps `finished_at` on success.
- Nudge: *"Branch pushed. After merge/review, run `mship close` to clean up."*

The existing `--base`, `--base-map`, and `--handoff` flags are incompatible with `--push-only` — passing them together exits 1 with a clear message.

## `mship close`

Replaces `mship abort` entirely. Same cleanup operation (remove worktrees, clear `current_task`, stamp `closed_at` for history), but routes on PR state.

### Resolution matrix

For a task with `pr_urls` non-empty, run `gh pr view <url> --json state -q .state` per PR and combine:

| Combined PR state | Behavior | Task log entry |
|---|---|---|
| all `MERGED` | Clean up, exit 0 | `closed: completed (N PRs merged)` |
| all `CLOSED` (unmerged) | Clean up, exit 0 | `closed: cancelled on GitHub` |
| any `OPEN`, no `--force` | Refuse, exit 1 | no entry written |
| any `OPEN`, `--force` given | Clean up, exit 0 | `closed: forced with open PRs (N open)` |
| at least one merged + at least one closed unmerged (no opens) | Clean up, exit 0 | `closed: mixed (N merged, M closed)` |

For a task with `pr_urls` empty (never finished, or finished with `--push-only`):

| Condition | Task log entry |
|---|---|
| `finished_at` is set | `closed: no PRs (pushed via --push-only)` |
| `finished_at` is unset | `closed: cancelled before finish` |

### Flags

- `--force` — bypass the open-PR guardrail. Refuses to silently skip the PR-state check (still invokes gh to produce accurate log entry, but doesn't gate on the result).
- `--yes` — skip the "Remove N worktrees?" confirmation in TTY mode. Non-TTY always proceeds. Matches the current `abort --yes` ergonomics.
- `--skip-pr-check` — skip the gh call entirely, log as `closed: pr state unchecked`. For offline / no-gh scenarios. Doesn't imply `--force`; an empty `pr_urls` still closes normally.

### Errors

- Missing current task → exit 1 with "No active task to close."
- `gh` not installed and `pr_urls` non-empty and neither `--skip-pr-check` nor `--force` given → exit 1 with "gh CLI needed to check PR state. Install gh, or pass --skip-pr-check."
- Any worktree removal failure → same best-effort behavior as today's abort: log the failure, continue, exit 0 if state cleanup succeeded.

## Phase Guardrails on Finished Tasks

When `task.finished_at` is set, these transitions refuse without `--force`:

- `mship phase plan`
- `mship phase dev`
- `mship phase review`

Allowed: `mship phase run` (running services against a finished task is legit for post-merge smoke tests).

Refusal message: *"Task X is finished (N ago). Transitioning to <phase> probably means you want `mship close` and then `mship spawn` for the next task. Use --force to override."*

`--force` bypasses; the transition is logged as `phase <x> (forced; task was finished)`.

The existing `--force` flag on `phase` (which clears a block) now also clears the finished-guardrail. Docstring updated.

## `mship status`

### New lines (in order, below existing output)

```
Task: add-labels-to-tasks
Phase: dev for 3h 12m
Branch: feat/add-labels-to-tasks
Repos: shared, auth-service
Worktrees:
  shared: /path
  auth-service: /path
Tests:
  shared: pass
  auth-service: pass
Drift: clean
Last log: "implemented avatar upload endpoint" (12m ago)
```

For a finished task:

```
Task: add-labels-to-tasks
⚠ Finished: 2h ago — run `mship close` after merge
Phase: review (entered 2h ago)
Branch: ...
```

### Data sources

- **Phase duration** — `datetime.now(UTC) - task.phase_entered_at`. Rendered with a tight helper (`5m`, `3h 12m`, `2d 4h`). If `phase_entered_at` is `None` (legacy task loaded from older state), print `Phase: dev` without the duration.
- **Drift** — a new `audit_repos(..., local_only=True)` mode that skips the `git fetch` + `@{u}` probes (so skips `fetch_failed`, `no_upstream`, `behind_remote`, `ahead_remote`, `diverged`). Keeps `path_missing`, `not_a_git_repo`, `detached_head`, `unexpected_branch`, `dirty_worktree`, `extra_worktrees`. Scoped to `task.affected_repos`. Summary line: `clean` (no error issues) or `N error(s) — run mship audit` with error count.
- **Last log** — `log_manager.read(slug, last=1)`. First line of the message, truncated to 60 chars. Timestamp rendered as relative duration (same helper).
- **Finished** — `task.finished_at`, relative duration.

### JSON mode

Adds fields to the existing JSON payload:

```json
{
  ...existing...,
  "finished_at": "2026-04-13T14:00:00Z",
  "phase_entered_at": "2026-04-13T11:00:00Z",
  "drift": {
    "has_errors": false,
    "error_count": 0
  },
  "last_log": {
    "message": "implemented avatar upload endpoint",
    "timestamp": "2026-04-13T13:48:00Z"
  }
}
```

Missing data → field is `null`.

## `mship view status`

Textual TUI already renders what `mship status` produces. Extends the existing `gather()` to include the same four new lines. No layout changes.

## Architecture & File Touch

**New:**
- `src/mship/util/duration.py` — `format_relative(dt: datetime) -> str` ("12m ago", "3h 12m ago", "2d 4h ago"). Small, pure, one test module.

**Modify:**
- `src/mship/core/state.py` — two new optional `datetime` fields on `Task`.
- `src/mship/core/phase.py` — set `phase_entered_at` on every transition; guard against finished tasks without `--force`.
- `src/mship/core/pr.py` — new `check_pr_state(pr_url) -> Literal["open","closed","merged","unknown"]` via `gh pr view --json state`.
- `src/mship/core/repo_state.py` — add `local_only: bool = False` kwarg to `audit_repos` and `_probe_git_wide`.
- `src/mship/cli/worktree.py` — in `finish`: stamp `finished_at`, add `--push-only`; add `close` command (rename `abort` → `close`); remove the `abort` command entry point.
- `src/mship/cli/status.py` — extend output with drift/last-log/phase-duration/finished lines.
- `src/mship/cli/view/status.py` — same, inside `StatusView.gather`.
- `skills/working-with-mothership/SKILL.md` — rename `abort` → `close`, document `--push-only`, document finished-task warnings.
- `README.md` — same vocabulary update.

**Delete:**
- No files deleted. The `abort` command function goes away but its surrounding module is shared with `finish`/`spawn`.

## Testing

### Unit tests

- `util.duration.format_relative` — 0s, 59s, 5m, 3h 12m, 2d 4h, >30d ("30+ days ago"), future dates ("just now"), tz-aware vs naive.
- `PRManager.check_pr_state` — parses `OPEN`/`CLOSED`/`MERGED`/unknown; tolerates gh failure (returns `unknown`).
- `audit_repos(local_only=True)` — skips fetch call (assert via mock counter); still emits dirty/branch/worktree issues; does not emit fetch-family issues even when tracking is broken.
- `Task` model accepts both legacy state (no `finished_at`/`phase_entered_at`) and fully-populated state.

### Integration tests

- `mship finish` success stamps `finished_at` and prints the close nudge.
- `mship finish --push-only` skips gh calls (assert no `gh pr create` in call log), pushes, stamps `finished_at`.
- `mship finish --push-only --base main` exits 1 (incompatible).
- `mship close` on task with all-merged PRs: cleans, logs `closed: completed`.
- `mship close` on task with all-closed-unmerged PRs: cleans, logs `closed: cancelled on GitHub`.
- `mship close` on task with mixed states without `--force`: exits 1.
- `mship close --force` on task with open PRs: cleans, logs `closed: forced with open PRs (1 open)`.
- `mship close` on task with no PRs and `finished_at` unset: logs `closed: cancelled before finish`.
- `mship close` on task with no PRs and `finished_at` set: logs `closed: no PRs (pushed via --push-only)`.
- `mship phase dev` on finished task without `--force` → exits 1 with the guardrail message.
- `mship phase run` on finished task → allowed.
- `mship phase dev --force` on finished task → allowed, logs the forced transition.
- `mship status` TTY output contains drift, last-log, phase-duration, and (on finished task) the finished warning.
- `mship status --json` includes the new fields.

## Migration Notes

- **Rename `abort` → `close`:** breaking. Skill and README updated. No deprecation period (pre-1.0).
- **Task fields:** backward-compatible. Older state files load with `None` for the new fields; the CLI handles `None` by omitting the derived lines.
- **`abort` shim:** not provided. `mship abort` fails with `No such command`. Acceptable pre-1.0.

## Out of Scope (post-v1)

- `mship close --wait` polling until PRs merge.
- Automatic `mship close` scheduled hook after merge webhook.
- Workspace-level `push_only: true` default for non-PR teams (covered by always passing `--push-only` today; revisit when someone asks).
- Shell prompt integration that shows worktree/finished status in PS1.
- `mship abort` deprecation shim that prints a migration hint. Acceptable pre-1.0.
