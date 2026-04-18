# Reconcile gate allows settled tasks — Design

## Context

After merging a PR on GitHub, the user returns to their workstation and runs `mship finish` (or `mship spawn`) — and the command refuses with:

```
ERROR: `mship finish` refused — upstream drift on:
  - <slug>: merged (PR #N)
Run `mship reconcile` for details, then fix or pass --bypass-reconcile.
```

Reached for in a real session (observed minutes ago in this workspace): `mship finish` blocks because a previously-finished-and-now-merged task still lives in `state.tasks`. The gate's drift matrix says `finish + merged = block`. The user's only remedy today is `mship close` (tears down the worktree) or `--bypass-reconcile` (defeats the gate). Both are friction for a case that should self-heal.

Investigation: the reconcile cache's persistence is a red herring. `_decisions_from_cache` already iterates `state.tasks`, so cache entries for truly-removed tasks are ignored. The real issue is the gate treats "merged + still-in-state" the same as "merged + user hasn't run finish yet," and that asymmetry is wrong. When a task has `finished_at` set AND its PR is merged or closed, the user's intent is clearly done — they've completed the task; only worktree teardown remains, which is the `close` command's job, not `finish`'s concern.

## Goal

The reconcile gate auto-allows `spawn` and `finish` commands when a task is **settled**: `finished_at` is set in `state.tasks[slug]` AND reconcile reports `state ∈ {merged, closed}`. The user no longer needs `--bypass-reconcile` or an interstitial `mship close` to unblock new work after a PR merges.

Visibility of settled-but-unclosed tasks stays via existing output: `mship reconcile` already prints them with state=merged. No inline hints on other commands.

## Success criterion

With one active task `foo` whose PR #42 is merged on GitHub and whose `finished_at` is set:

```
$ mship finish
<opens PRs for any remaining tasks with unfinished state, or completes with "nothing to push">
$ echo $?
0
```

No `"upstream drift on: foo: merged"` error. No `--bypass-reconcile`. The settled task stays in `state.tasks` until the user runs `mship close --task foo` explicitly.

`mship reconcile` on the same state still prints:
```json
{"tasks": [{"slug": "foo", "state": "merged", "pr_url": "...", ...}], ...}
```

No change to reconcile's JSON — users who want to see settled tasks run reconcile.

## Anti-goals

- **No auto-`close`.** Tearing down worktrees and deleting branches is `close`'s job. Auto-invoking it from `finish` / `spawn` risks destroying user work (dirty worktree, unpushed commits), and `close` has its own safety chain (recovery-path check, base-ancestry check, finish-required check) that would have to be replicated or skipped.
- **No change to `precommit` gate action.** Committing on a branch whose PR has already merged is an unusual state; blocking the commit is still the right call.
- **No change to `close` gate action.** Already `allow` for merged.
- **No new CLI output on `spawn`/`finish`.** Per brainstorming Q2-B — settled tasks are visible via existing `mship reconcile` JSON. Adding inline hints on every gate-running command would be the kind of chatter this line of work (issues #51, #35) has been tightening.
- **No cache-file format change.** `finished_at` is plumbed at Decision-construction time from fresh state, not persisted. Avoids migration.
- **No new CLI flag** to opt into or out of the auto-allow behavior. Adding flags to undo noisy defaults is a worse design than fixing the default.
- **No broadening beyond `state ∈ {merged, closed}`.** `diverged`, `base_changed`, `missing`, `in_sync` all stay on the current matrix. "Settled" strictly means the PR has completed its lifecycle.

## Architecture

### `src/mship/core/reconcile/gate.py::Decision` — new field

```python
@dataclass(frozen=True)
class Decision:
    slug: str
    state: UpstreamState
    pr_url: str | None
    pr_number: int | None
    base: str | None
    merge_commit: str | None
    updated_at: str | None
    finished_at: str | None = None  # ISO-8601 string if task.finished_at is set
```

Defaulted to `None` so existing test-fixture constructors don't need updating.

### `src/mship/core/reconcile/gate.py` — plumb `finished_at` from state

New helper:

```python
def _finished_at_for(slug: str, state: WorkspaceState) -> str | None:
    task = state.tasks.get(slug)
    if task is None or task.finished_at is None:
        return None
    return task.finished_at.isoformat()
```

Update the two Decision constructors to accept `state` and populate:

```python
def _decision_from_detection(slug: str, det: Detection, state: WorkspaceState) -> Decision:
    return Decision(
        slug=slug, state=det.state, pr_url=det.pr_url, pr_number=det.pr_number,
        base=det.base, merge_commit=det.merge_commit, updated_at=det.updated_at,
        finished_at=_finished_at_for(slug, state),
    )


def _decision_from_cache_entry(slug: str, raw: dict, state: WorkspaceState) -> Decision | None:
    try:
        return Decision(
            slug=slug,
            state=UpstreamState(raw["state"]),
            pr_url=raw.get("pr_url"),
            pr_number=raw.get("pr_number"),
            base=raw.get("base"),
            merge_commit=raw.get("merge_commit"),
            updated_at=raw.get("updated_at"),
            finished_at=_finished_at_for(slug, state),
        )
    except (KeyError, ValueError):
        return None
```

`reconcile_now` already has `state`; pass it through at both call-sites:

```python
# From cache:
for slug in state.tasks:
    raw = payload.results.get(slug)
    if raw is None:
        continue
    d = _decision_from_cache_entry(slug, raw, state)
    ...

# From fresh detection:
return {slug: _decision_from_detection(slug, d, state) for slug, d in detections.items()}
```

### `src/mship/core/reconcile/gate.py::should_block` — auto-allow for settled

```python
def should_block(decision: Decision, *, command: Command, ignored: list[str]) -> GateAction:
    if decision.slug in ignored:
        return GateAction.allow
    if (
        decision.finished_at is not None
        and decision.state in (UpstreamState.merged, UpstreamState.closed)
        and command in ("spawn", "finish")
    ):
        return GateAction.allow
    return _MATRIX[decision.state.value][command]
```

The `_MATRIX` stays untouched. The early-return captures the new case without muddying the declarative matrix — easier to read than a mutation or a cross-product expansion.

### No other modules touched

- `src/mship/core/reconcile/cache.py` — unchanged. `finished_at` is not cached.
- `src/mship/core/reconcile/detect.py` — unchanged.
- `src/mship/core/reconcile/fetch.py` — unchanged.
- CLI handlers (`spawn`, `finish`, `close`, `reconcile`) — unchanged. They call `should_block` through existing paths.

## Data flow

**User merges PR #42 on GitHub, then runs `mship finish`:**

1. CLI `finish` → `_run_gate(..., command="finish")` → `reconcile_now(state, cache=..., fetcher=...)`.
2. `reconcile_now` loads state, constructs Decisions. For task `foo` with `finished_at` set and PR state `merged`, `_decision_from_detection` populates `Decision.finished_at = "2026-04-18T13:20:28+00:00"`.
3. Gate iterates decisions, calls `should_block(decision, command="finish", ignored=[])`.
4. Settled early-return fires → `GateAction.allow`.
5. Gate aggregates — no blocks → `finish` proceeds. If there are no new PRs to create, finish completes with existing no-op behavior.

**`mship reconcile` on the same state:**

1. Same `reconcile_now` path.
2. Decisions returned to CLI, which serializes to JSON. The `finished_at` field is NOT exposed in the JSON output (the serializer doesn't know about it, and adding it is out of scope — users already infer settlement from `state`).
3. User sees the settled task in the output with state=merged. They know they can `mship close` when they're ready.

**Existing behavior paths unchanged:**

- Task with state=merged but no `finished_at` (user merged externally without `mship finish`): early-return skipped → MATRIX fires → block. Same as today.
- Task with `finished_at` set but state=in_sync (PR still open): MATRIX says allow for finish. Same as today.
- Task with `finished_at` set and state=diverged (merged-then-new-commits-upstream): MATRIX says block for finish. Same as today. Edge case; acceptable.

## Error handling

- **Task not in `state.tasks`**: `_finished_at_for` returns `None` → early-return doesn't fire → MATRIX applies. Safe fallback; shouldn't happen in practice (decisions are constructed by iterating `state.tasks`).
- **`task.finished_at` is not a `datetime`**: the State model (`mship.core.state.Task.finished_at: datetime | None`) types this as `datetime`. If a malformed state file produced a string, `.isoformat()` would raise. Defensively, we could `try/except`, but the state file is mship-owned — malformed files are a bigger problem than this one check. Let it raise.
- **Race: PR merges between reconcile fetch and gate check**: not a new problem. Cache TTL is 300s; user accepts fresh-enough state. If merge happens in that window and user's `finish` fires, the new logic still correctly allows because (a) finished_at was set at finish-time, and (b) if reconcile sees merged in the next fetch, the new allow fires.

## Testing

### Unit — `tests/core/reconcile/test_gate.py` (extend existing)

1. **Backward compat**: `should_block(Decision(slug="x", state=merged, finished_at=None, ...), command="finish", ignored=[])` → `block` (MATRIX behavior unchanged for tasks that haven't been finished).
2. **Merged + finished + finish → allow**: with `finished_at="2026-04-18T13:20:28+00:00"`, same call → `allow`.
3. **Merged + finished + spawn → allow**: same Decision, command="spawn" → `allow`.
4. **Merged + finished + precommit → block**: same Decision, command="precommit" → `block` (scope boundary — unchanged from MATRIX).
5. **Merged + finished + close → allow**: same Decision, command="close" → `allow` (MATRIX already says this; regression guard).
6. **Closed + finished + finish → allow**: `state=closed, finished_at="..."` → `allow` (covers the closed-PR branch of the early-return).
7. **In_sync + finished + finish → MATRIX path (allow)**: early-return doesn't fire because state isn't merged/closed; MATRIX applies.
8. **Diverged + finished + finish → block**: MATRIX fires → block. Regression guard that the early-return isn't over-eager.
9. **Ignored slug overrides everything**: `should_block(..., ignored=["x"])` returns `allow` even when state would block. Existing test; verify it still passes.

### Unit — `reconcile_now` populates `finished_at`

New tests in `tests/core/reconcile/test_gate.py` (or a new file if preferred):

10. **Fresh fetch path with finished task**: build a `WorkspaceState` with `tasks={"foo": Task(slug="foo", finished_at=datetime(...), branch="feat/foo", base_branch="main", affected_repos=[...], worktrees={...})}`. Mock fetcher returns a merged PR for `foo`. Call `reconcile_now(state, cache=<empty>, fetcher=<mock>)`. Assert the returned `Decision.finished_at` equals `task.finished_at.isoformat()`.

11. **Cache-hit path with finished task**: pre-populate cache with a results payload for `foo`. Call `reconcile_now` (fresh cache → skips fetch). Assert the returned `Decision.finished_at` is the task's isoformat (plumbed from state at decision-construction, not from cache).

12. **Finished_at=None path**: build state with `finished_at=None`. Assert `Decision.finished_at is None`.

13. **Slug in state but `finished_at is None`**: scenario: task is mid-work, PR opened but `mship finish` hasn't run (weird edge case). Decision.finished_at = None. should_block fires MATRIX. Regression guard.

### Integration — manual smoke

In a workspace with the feature installed:

1. Create a task, run `mship finish` to open a PR.
2. Merge the PR on GitHub.
3. Run `mship finish` again. Expect: no `"upstream drift"` error, command completes cleanly (even if there's nothing to do).
4. Run `mship reconcile`. Expect: JSON still shows the settled task with `state: "merged"`.
5. Run `mship close --task <slug>`. Expect: normal close flow (worktree teardown + state removal).

### Regression

- All existing `tests/core/reconcile/test_gate.py` tests pass without modification. `Decision(..., finished_at=None)` default preserves existing constructor calls.
- `tests/cli/*` tests that exercise the gate indirectly must still pass. If a test constructs tasks with `finished_at` AND mocks reconcile to return `merged`, the new logic applies — update the test to either set `finished_at=None` (preserve old block behavior) or expect the new allow (if that's what the test is proving). Both paths are correct; it depends on what the test was asserting.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Filter at `should_block`, not at state level | Keeps `state.tasks` authoritative. User can still `mship close --task <slug>` explicitly; nothing is auto-destroyed. |
| 2 | Plumb `finished_at` into Decision, not into `should_block` directly | Decision becomes the single source of truth for gate inputs. Adding a sidecar parameter would couple the gate caller to state shape. |
| 3 | Only `spawn` and `finish` auto-allow; `precommit` stays `block` | Committing on a merged branch is unusual user state — the block is a useful signal. `close` already allows. |
| 4 | No CLI output changes | Per Q2-B — settled tasks are visible via `mship reconcile`. Chatter on every gate-running command was the anti-pattern this line of work is tightening. |
| 5 | Don't cache `finished_at` in the reconcile cache file | `finished_at` comes from state, which is cheap to read. Caching would add a migration step and another place to stale. |
| 6 | `finished_at` serialized as ISO-8601 string, not datetime | Consistency with other string-or-None fields on Decision; keeps the dataclass hashable (frozen=True); avoids timezone-handling surprises at comparison time. |
| 7 | Use an `if … in (merged, closed)` tuple check in should_block, not a new MATRIX entry | The MATRIX is declarative — adding a "settled" pseudo-state would require state to carry settlement as an enum value, which requires plumbing. Early-return is surgical. |
