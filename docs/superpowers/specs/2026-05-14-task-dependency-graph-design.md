# Task dependency graph: `depends_on` edges between tasks

**Date:** 2026-05-14
**Issue:** [#104](https://github.com/atomikpanda/mothership/issues/104)
**Status:** Design approved; ready for implementation plan.

## Problem

Tasks in mship are islands. There is no first-class representation that "task B depends on task A" — the dependency lives in the user's head and surfaces as ad-hoc coordination:

- Shared-lib refactor in task A → consumer update in task B: user manually watches A's PR, rebases B before starting.
- Schema migration → app code → docs: a three-step chain the user sequences by memory.
- Spec work in task A produces a decision task B depends on: no linkage beyond journal prose.

This is the same class of "state hell" mship is built to prevent, but at the cross-task level instead of cross-repo. Worktree isolation solved the intra-task version; the inter-task version is still manual.

The substrate value: once the graph exists as state, every existing command (`status`, `finish`, `dispatch`, `reconcile`, `close`) gets more powerful without redesign.

## Goals

- First-class `depends_on` edges between tasks, persisted in `state.yaml`.
- Hard edges block `finish` until the upstream is merged; soft edges advise.
- `status`, `dispatch`, `reconcile` surface the graph so a single agent or human can act on it.
- Cycle detection at write time.
- Lay the v1 primitive that v2 multi-agent orchestration will sit on top of, without speculative design.

## Non-goals

- **No automatic rebasing** when an upstream merges. Detect staleness via `reconcile`; user runs the rebase.
- **No GitHub-level sync.** Mship doesn't read "Depends on #N" from PR bodies or write them back. The graph is mship's.
- **No critical-path / scheduling logic.** Mship records the graph; downstream tools can compute critical path if they want it.
- **No parallel-execution orchestration.** That's v2 multi-agent territory.
- **No cross-repo edges.** Edges are task-to-task; the per-repo axis stays out.
- **No OR / alternative-groups fan-in.** Default is AND (all upstream must be ready).
- **No PR-URL endpoints.** Edges name task slugs only. External-PR dependencies are a v2 cross-workspace concern.

## Design

### State model

Additive on `Task` in `src/mship/core/state.py`:

```python
class DependencyEdge(BaseModel):
    upstream_slug: str
    soft: bool = False           # default: hard
    created_at: datetime

class Task(BaseModel):
    # ... existing fields ...
    depends_on: list[DependencyEdge] = []
```

Edges live on the **downstream** task. Reverse lookup ("which tasks depend on me?") is computed by iterating `state.tasks` — O(N) at workspace scale, fine. `WorkspaceState` already has `extra="ignore"`, so legacy `state.yaml` files load cleanly with `depends_on=[]`.

No schema migration. The field is additive and defaults to `[]`.

### Readiness signal

An upstream task is "ready" iff `reconcile` reports it as fully merged across every repo in its `affected_repos`:

- For each repo in `upstream.affected_repos`, the existing `reconcile.gate` machinery decides an `UpstreamState`. Ready ⇔ every repo is `UpstreamState.merged`.
- A task with `finished_at` set but PRs not yet merged is **not** ready. Finishing creates PRs; readiness requires merge.
- A soft edge never blocks `finish`; it only advises.

This reuses the existing reconcile decision logic. No new readiness machinery; no parallel state tracker.

### CLI surface

One new top-level verb group, three subcommands:

```
mship depends add <upstream-slug> [--soft] [--task <slug>]
mship depends remove <upstream-slug> [--task <slug>]
mship depends list [--task <slug>] [--graph]
```

- `--task <slug>` defaults to the cwd-resolved task (same resolution rules as `status`/`finish`).
- `--graph` on `list` emits the full workspace DAG (all tasks, all edges) regardless of `--task`. Without `--graph`, `list` shows only the resolved task's upstream + downstream. JSON shape in non-TTY, simple text rendering in TTY.

Extensions on existing commands:

- `mship spawn --depends-on <slug>[,<slug>]` — hard edges by default.
- `mship spawn --depends-on-soft <slug>[,<slug>]` — soft edges.
- `mship finish` — refuses if any **hard** upstream isn't ready. Bypass flag: `--bypass-deps` (per memory: `--bypass-<check>` naming).
- `mship close` — when downstream tasks exist:
  - TTY: interactive prompt with `[c]ascade-close downstream`, `[d]etach edges`, `[a]bort`.
  - Non-TTY: refuses; requires `--cascade` (cascade-close downstream) or `--detach-downstream` (clear inbound edges, leave downstream alive).
  - The existing `--force` flag continues to bypass all checks including this one; no new `--bypass-deps` on close — `--cascade` and `--detach-downstream` cover the non-destructive paths.
- `mship status` — adds under `resolved_task`:
  ```json
  "dependencies": {
    "upstream": [
      {"slug": "task-a", "soft": false, "ready": true},
      {"slug": "task-b", "soft": true,  "ready": false}
    ],
    "downstream": [
      {"slug": "task-c", "soft": false}
    ],
    "blocked": false,
    "blocked_by": []
  }
  ```
  `blocked` is `true` iff any hard upstream is not ready. `blocked_by` lists those slugs. Existing envelope shape is unchanged; this is one new key under `resolved_task`.
- `mship dispatch` — prompt body grows a `## Dependencies` section listing upstream slugs and their ready state. Whether the agent task-switches when blocked is methodology — surfaced in the `working-with-mothership` skill, not in CLI logic.
- `mship reconcile` — adds a new state `dependency_stale` to `UpstreamState`, surfaced when an upstream became `merged` AFTER the downstream task was created (downstream needs rebase). Same `Decision` shape; one new enum value.

### Cycle detection

At write time (`spawn --depends-on`, `depends add`):

1. Compute the transitive upstream set of the proposed upstream via DFS over `Task.depends_on`.
2. If the current task is in that set → refuse with `CycleError`, printing the full cycle path: `task-c → task-b → task-a → task-c`.
3. Self-edges (`task-a` depending on itself) are rejected as a degenerate cycle.

Bounded by workspace task count. Cycle checking happens before persistence; no half-written state.

### Composition with existing primitives

| Command | New behavior |
|---|---|
| `spawn` | `--depends-on` / `--depends-on-soft` create edges at spawn time. Cycle check runs. |
| `finish` | Refuses if any hard upstream not ready. `--bypass-deps` overrides. |
| `close` | Refuses (or prompts) when downstream tasks exist. `--cascade` / `--detach-downstream`. (`--force` covers the destructive path.) |
| `status` | Emits `dependencies` block under `resolved_task`. |
| `dispatch` | Includes upstream slugs + ready state in the prompt body. |
| `reconcile` | New `dependency_stale` state when upstream merged after downstream created. |
| `depends` | New verb group: `add` / `remove` / `list`. |

### Error handling

- **Unknown upstream slug** at write time → loud error listing available slugs in the workspace.
- **Cycle** → loud error with explicit cycle path.
- **Self-edge** → loud error (degenerate cycle).
- **Removing an edge that doesn't exist** → loud error; do not silently no-op.
- **Finish blocked by upstream** → error names the offending upstream(s) and their state; hints at `mship status --task <upstream>` and `--bypass-deps`.
- **Close with downstream, non-TTY, no flag** → refuses; lists downstream slugs and instructs to pass `--cascade` or `--detach-downstream`.

## Files touched

- **Modified:** `src/mship/core/state.py` — add `DependencyEdge`; add `depends_on` field on `Task`.
- **Modified:** `src/mship/core/graph.py` — already exists; add cycle detection + readiness queries (`is_ready(slug)`, `downstream_of(slug)`, `transitive_upstream(slug)`).
- **Modified:** `src/mship/cli/worktree.py` — `spawn` flags (`--depends-on`, `--depends-on-soft`); `finish` blocked-by-deps check; `close` downstream check with cascade/detach options.
- **Modified:** `src/mship/cli/__init__.py` — register the `depends` subcommand group.
- **New:** `src/mship/cli/depends.py` — `add`, `remove`, `list` commands.
- **Modified:** `src/mship/cli/status.py` — emit `dependencies` block under `resolved_task`.
- **Modified:** `src/mship/core/dispatch.py` — add `## Dependencies` section to prompt template.
- **Modified:** `src/mship/core/reconcile/detect.py` + `src/mship/core/reconcile/gate.py` — extend `UpstreamState` with `dependency_stale`; emit it when applicable.
- **Modified:** `src/mship/cli/reconcile.py` — render the new state in TTY + JSON output, add an action hint.
- **Docs:** `working-with-mothership` skill, `README.md`, `AGENTS.md`, `GEMINI.md` — document the new verb, `--depends-on*` flags, and `status.resolved_task.dependencies` shape.

## Testing strategy

Unit (`tests/core/test_graph.py`):

1. **Cycle detection:** self-edge rejected; 2-node cycle rejected; 3-node cycle rejected; diamond DAG (A → B, A → C, B → D, C → D) accepted with no false-positive.
2. **Readiness:** upstream with all repos merged → ready; one repo unmerged → not ready; soft edge → blocking-irrelevant.
3. **Transitive upstream:** correct closure across multi-hop chains.

CLI (`tests/cli/`):

4. `spawn --depends-on task-a` creates the edge; unknown upstream errors loudly.
5. `spawn --depends-on task-a` where `task-a` doesn't exist → loud error listing workspace task slugs.
6. `depends add task-a` / `depends remove task-a` / `depends list` round-trip on an existing task.
7. `depends add task-a --soft` produces `soft: true` in state.
8. `finish` on a task with an unready hard upstream → refuses with named upstream.
9. `finish --bypass-deps` succeeds even with unready upstream.
10. `close` on a task with downstream, non-TTY, no flag → refuses with downstream slugs listed.
11. `close --cascade` removes both tasks; downstream worktrees torn down.
12. `close --detach-downstream` leaves downstream alive with empty `depends_on` for the removed edge.
13. `status` envelope: `dependencies.blocked = true` when hard upstream unready; downstream list populated; soft edges visible but never set `blocked`.
14. `dispatch` payload contains a `## Dependencies` section with upstream slugs and ready state.

Integration (one):

15. `spawn task-a → spawn task-b --depends-on task-a → finish task-b fails (blocked) → finish task-a → reconcile → finish task-b succeeds`.

## Risk and rollout

- **Backward compatibility:** Additive field on `Task` with default `[]`. Existing `state.yaml` files load unchanged. `extra="ignore"` on `WorkspaceState` covers the read side.
- **Behavior change on `finish` and `close`:** A workspace with no edges sees zero behavior change. Behavior change only activates once edges exist — opt-in by user action.
- **`status` envelope change:** One new key (`resolved_task.dependencies`). Additive — no consumer breakage. Existing keys unchanged.
- **`reconcile.UpstreamState` enum change:** Adds one variant (`dependency_stale`). Consumers using `UpstreamState.value` get a new string they may not handle; the reconcile JSON shape is otherwise unchanged. Note in PR body.
- **`mship depends` verb collision:** Verified — no existing `depends` subcommand.
- **No deprecation window needed.** All changes additive on existing surfaces; new commands are new.

## Open questions

None at design time. The 8 design questions in #104 are all resolved:

1. **Target:** slug only.
2. **Hard vs soft:** hard by default; per-edge `--soft` opt-in.
3. **Fan-in:** AND (all upstream must be ready).
4. **Cascade:** TTY-interactive prompt; non-TTY refuses without `--cascade` or `--detach-downstream`.
5. **Dispatch autonomy:** CLI surfaces blocked state only; task-switching is methodology in the skill.
6. **Cycle detection:** at write time, with explicit path in error.
7. **Migration:** additive field defaults to `[]`; `mship depends add/remove` retrofits.
8. **Cross-repo edges:** out of scope; task-to-task only.

The `mship task` verb group was reconsidered and declined again, per the 2026-05-08 status-envelope spec's prior rejection. Single-purpose `mship depends` follows the existing pattern (`mship debug`, `mship bind`, `mship view`).
