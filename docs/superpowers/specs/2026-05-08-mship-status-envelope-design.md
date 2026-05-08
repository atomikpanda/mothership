# `mship status`: single-envelope JSON shape

**Date:** 2026-05-08
**Issue:** [#128](https://github.com/atomikpanda/mothership/issues/128)
**Status:** Design approved; ready for implementation plan.

## Problem

`mship status` currently returns one of two JSON shapes depending on whether a
task can be resolved from context:

- **Workspace summary** (no task anchored, or 0/2+ active tasks):
  `{"active_tasks": [...]}`
- **Task detail** (cwd in worktree, `MSHIP_TASK` set, or exactly one active task):
  the task object directly (`{"slug", "phase", "worktrees", ...}`).

JSON consumers (agents, scripts, CI) must branch on response shape to know which
case they're in. There's no shared envelope or discriminator. Every consumer
pays this tax — the bundled `working-with-mothership` skill has to explain both
shapes; CI integrations break when context shifts; agent prompts have to defend
against the polymorphism. A consumer that assumes one shape and gets the other
crashes on a missing key.

## Goals

- One stable JSON shape for `mship status`, always.
- Workspace data and resolved-task detail both reachable from the same payload.
- Backward-compatible additive **for the workspace shape** (existing
  `.active_tasks[]` consumers continue to work). Hard flip on the task-detail
  side: scripts doing `mship status | jq .phase` migrate to
  `mship status | jq .resolved_task.phase`.

## Non-goals

- No new top-level verbs. No `mship task` command. (Earlier draft proposed one;
  rejected to avoid verb pollution.)
- No change to TTY rendering. Humans see the existing context-aware output
  (workspace summary or task detail). Polymorphism in TTY is a UX feature, not
  a bug.
- No deprecation warning window. Hard flip on the wire — same posture as the
  recent #108 / #135 fix where loud breakage beat silent dual-mode cruft.

## Design

### One envelope, always

`mship status` (non-TTY) emits exactly this shape regardless of context:

```json
{
  "workspace": "<name from mothership.yaml>",
  "active_tasks": [
    {
      "slug": "feat-x",
      "phase": "dev",
      "branch": "feat/feat-x",
      "phase_entered_at": "2026-05-08T12:34:56+00:00"
    },
    ...
  ],
  "resolved_task": null,
  "resolution_source": null,
  "cwd_is_outside_worktrees": false
}
```

When a task resolves (cwd > `MSHIP_TASK` > only-active > `--task X`):

```json
{
  "workspace": "...",
  "active_tasks": [...],
  "resolved_task": {
    "slug": "feat-x",
    "phase": "dev",
    "branch": "feat/feat-x",
    "worktrees": {...},
    "test_results": {...},
    "pr_urls": {...},
    "phase_entered_at": "...",
    "blocked_reason": null,
    "finished_at": null,
    "active_repo": null,
    "test_iteration": 0,
    "base_branch": "main",
    "drift": {"has_errors": false, "error_count": 0},
    "last_log": {"message": "...", "timestamp": "..."} | null
  },
  "resolution_source": "cwd",
  "cwd_is_outside_worktrees": false
}
```

The `resolved_task` object is the full task-detail payload that today's
`mship status` returns at the top level when a task resolves. It includes:

- All `Task` model fields (`slug`, `phase`, `branch`, `worktrees`,
  `test_results`, `pr_urls`, `affected_repos`, etc.).
- `drift`: the local-only audit summary currently emitted at top level.
- `last_log`: the most recent journal entry's first-line preview + timestamp,
  currently emitted at top level.

### Resolution rules unchanged

Same precedence as today: `--task X` > cwd inside a worktree > `MSHIP_TASK`
env > exactly-one-active-task. When zero or multiple active tasks exist
without an anchor, `resolved_task` is `null` — **no error**, just no
resolution. Today's behavior already treats this as "fall back to workspace
shape"; the new design preserves that, just inside the same envelope.

`mship status --task X` continues to work: it forces `resolved_task` to be
task X (or errors with `UnknownTaskError` if X is unknown). The envelope
shape stays identical.

### TTY rendering — unchanged

The bifurcated TTY rendering stays:

- When `resolved_task` is null → render the workspace summary
  (`Active tasks (N): ...`, plus optional cwd-warning lines).
- When `resolved_task` is set → render the existing task-detail view
  (`Task: <slug>`, branch/phase/repos/drift/last log block).

Humans aren't parsing JSON; the context-aware UX is correct for terminals.

### Hard flip — no top-level duplication

Top-level keys (`slug`, `phase`, `worktrees`, etc.) **do not** mirror from
`resolved_task`. If you want detail, you read `.resolved_task.X`. Brittle
dual-mode duplication is what we're escaping.

A consumer's migration is mechanical:

```diff
-mship status | jq .phase
+mship status | jq .resolved_task.phase
```

Same for `.slug`, `.branch`, `.worktrees`, etc. Existing scripts that already
use `.active_tasks[]` are unchanged.

## Files touched

- **Modified:** `src/mship/cli/status.py` — unify the two JSON output branches
  into one envelope. Both today's "no task resolved" and "task resolved"
  branches now build the same envelope shape, populating `resolved_task`
  conditionally.
- **Modified:** `tests/cli/test_status.py` (and any other tests that assert
  the old top-level keys on the task-detail shape).
- **Docs:** `README.md`, `AGENTS.md`, `GEMINI.md`, the
  `working-with-mothership` skill — update `mship status | jq .X` examples to
  `mship status | jq .resolved_task.X`. Add a `BREAKING CHANGE:` line in the
  commit message and the PR body explaining the wire-shape change.

## Testing strategy

Unit-level (`tests/cli/test_status.py`):

1. With zero active tasks, `mship status` returns `{workspace, active_tasks:
   [], resolved_task: null, resolution_source: null, ...}` — every key
   present.
2. With one active task and cwd inside its worktree, `resolved_task` is the
   full task object, `resolution_source == "cwd"`, `active_tasks` still
   includes the slug.
3. With multiple active tasks and no anchor, `resolved_task` is `null`,
   `active_tasks` lists all slugs, `resolution_source` is `null`.
4. `--task X` overrides resolution: `resolved_task.slug == "X"` regardless of
   cwd.
5. `--task <unknown>` errors with `UnknownTaskError` (existing behavior, just
   confirm it survives the refactor).
6. Drift summary appears under `resolved_task.drift` (not top level).
7. Last-log preview appears under `resolved_task.last_log` (not top level).
8. TTY rendering is unchanged: with one resolved task, output contains the
   `Task: <slug>` block; with none, output contains `Active tasks (N):`.

Integration: existing `test_full_lifecycle` smoke test is updated to read
`.resolved_task.phase` instead of `.phase` after spawn.

## Risk and rollout

- **Wire breakage:** every script doing `mship status | jq .phase` (or any
  other task-detail key at top level) breaks the moment this lands. The
  breakage is loud — `null` from jq, or `KeyError` from Python consumers.
  The migration is one keystroke per consumer.
- **No deprecation window.** The dual-mode cruft from a soft-deprecation
  period is exactly what we're trying to leave behind. Ship clean, document
  loudly.
- **Mitigations:**
  - `BREAKING CHANGE:` in commit subject and PR body.
  - PR body includes the migration diff snippet.
  - Update the bundled `working-with-mothership` skill in the same PR so the
    documented examples are correct from the moment the change merges.

## Open questions

None at design time. The three earlier-flagged decision points
(verb shape, deprecation strategy, `--task` flag) are all resolved by this
design:

- No new verb (`mship task` rejected for verb pollution).
- Hard flip on JSON shape; no soft-deprecation window.
- `--task X` stays — composes naturally with the envelope now.
