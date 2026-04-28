# Hub Worktree Layout Design Spec

Resolves #87 (worktree layout breaks sibling-relative paths). Defers and partially obviates #110 (`external_symlinks`); see "Out of Scope" section.

## Overview

mship currently places each repo's worktrees inside that repo: `<repo>/.worktrees/feat/<slug>/`. This breaks every cross-repo path pattern that depends on siblings being siblings — editable Python deps (`pip install -e ../shared`), Taskfile cross-cd (`cd ../shared && task build`), docker-compose volume mounts, IDE workspace files, etc. mship is positioned as the coordination layer for cross-repo work, but the layout actively undermines the cross-repo coordination it's supposed to enable.

This spec replaces the per-repo layout with a **per-task hub layout**: every worktree for a task — including auto-materialized dependencies — lives as a sibling under `<workspace>/.worktrees/<slug>/`. Cross-repo references resolve correctly by construction.

The change is structural, not configurable. Per-repo layout is removed entirely; no opt-in/opt-out setting. mship is pre-1.0; the cost of carrying two layouts forever vastly exceeds the cost of a clean break now.

## 1. Hub Layout

### Layout

```
<workspace>/
├── mothership.yaml
├── api/                       ← canonical checkout (unchanged)
├── shared/                    ← canonical checkout (unchanged)
└── .worktrees/
    └── <slug>/                ← per-task hub
        ├── api/               ← affected: feat/<slug>
        ├── shared/            ← affected or passive (see §2)
        └── .mship-workspace   ← single marker per hub
```

`<slug>` is the existing branch slug. Branch name on each affected worktree stays `feat/<slug>` per current `branch_pattern`.

### Why this shape

- `cd ../sibling` from inside any worktree resolves to the sibling task worktree, by construction. No symlink layer, no scanner, no path-rewriting.
- A single `.mship-workspace` marker at the hub root is sufficient — any walk-up from inside any sibling lands on it. Subrepo workspace discovery (#84) keeps working.
- `git worktree add` semantics are unchanged. mship just passes a different destination path to git. No new git feature, flag, or invocation.

### Git plumbing

`git worktree add <hub>/<slug>/<repo> feat/<slug>` from each canonical repo. Git tracks each worktree by absolute path and writes a per-worktree admin dir at `<canonical-repo>/.git/worktrees/<slug>/` — exactly as today, just with a different destination path. `git worktree list`, `git worktree remove`, `git worktree move` all work normally.

### Workspace `.gitignore`

If the workspace root itself lives in a git repo (common when a single repo IS the workspace), mship adds `.worktrees` to that root's `.gitignore` on first spawn. Same logic as today's per-repo `.gitignore` handling, just at a different path.

### Removal of per-repo layout

- Drop the `worktree_layout` field from `mothership.yaml` (it never shipped — this is preemptive).
- All new spawns use hub layout unconditionally. No flag, no env var, no setting.
- In-flight tasks created before upgrade are unaffected (state.yaml stores absolute paths). They finish out under their original layout. See §4 for the migration story.

## 2. Passive Worktrees

A *passive worktree* is a sibling repo materialized in the hub because some affected repo declared `depends_on` on it, but the user didn't include it in `--repos`. It's read-only-by-convention (pre-commit hook refuses commits) and pinned to a stable origin ref.

### When materialized

At spawn, mship walks `depends_on` from each affected repo. Any transitive dep that isn't in `affected_repos` becomes passive. Same hub layout slot: `<workspace>/.worktrees/<slug>/<dep>/`.

`depends_on` is the only auto-materialization signal. Cross-repo references not modeled in `depends_on` (Taskfile cross-cd to a non-dep, ad-hoc scripts) require explicit declaration via the future `external_symlinks` feature (out of scope; see §5). This is a deliberate trade — auto-discovery via per-ecosystem scanners (Python pyproject, Node package.json, Taskfile, docker-compose) is a maintenance liability and gets things wrong; explicit declaration is principled and auditable.

### What branch they're on

For each passive repo, mship resolves the branch ref as:
- `expected_branch` if set in `mothership.yaml`, else
- `base_branch` if set, else
- **error**: "passive materialization for `<repo>` requires `expected_branch` or `base_branch` declared in mothership.yaml."

Then:

```
git fetch origin <ref>
git worktree add --detach <hub>/<slug>/<dep> origin/<ref>
```

**Detached HEAD at the just-fetched origin ref.** The local branch is never consulted, so "I forgot to fetch and got a stale passive worktree" is impossible by construction.

### Setup treatment

Materialize `symlink_dirs` (e.g., `node_modules`) and `bind_files` (e.g., `.env`). **Do NOT run `task setup`.** Rationale: the consumer's `cd ../shared && task build` typically only needs shared's source plus its already-installed deps (which `symlink_dirs` provides). `task setup` for passive deps would balloon spawn time for a benefit only realized in unusual cases (codegen-during-setup, schema generation).

A per-repo escape hatch (`passive_setup: skip | symlinks-only | full`, default `symlinks-only`) is **deferred** until a concrete use case appears. YAGNI.

### State.yaml schema

Existing `Task.worktrees: dict[str, Path]` stays one entry per repo (affected or passive). New field:

```python
class Task(BaseModel):
    worktrees: dict[str, Path]
    passive_repos: set[str] = set()  # subset of worktrees.keys()
    ...
```

`passive_repos` lets `mship status`, `mship audit`, and the pre-commit hook ask "is this passive?" without inferring it from branch state or path.

### Pre-commit hook

After the existing "matches a registered worktree" check passes, also check `repo_name in matched_task.passive_repos`. If so, refuse:

```
⛔ mship: refusing commit — <path> is a passive worktree of `<repo>`
   for task `<slug>`. To edit <repo>, close this task and respawn
   with `--repos <repo>,<other-affected> ...`
   (or `git commit --no-verify` to override).
```

### Offline escape hatch

`mship spawn --offline` skips the `git fetch` and uses the local `<ref>`. A journal entry tagged `OFFLINE` is written so the choice is audit-visible. Single flag, applies to all passive fetches in this spawn.

## 3. Lifecycle Integration

### `spawn`

1. Resolve affected repos from `--repos` (existing logic).
2. Walk `depends_on` graph; deps not in affected become passive.
3. Run audit (existing gate). For passive repos: `git fetch origin <ref>` and validate. Fetch failure blocks unless `--offline` (graceful) or `--force-audit` (logged bypass).
4. For affected repos: `git worktree add <hub>/<slug>/<repo> feat/<slug>` (existing flow, new path).
5. For passive repos: `git worktree add --detach <hub>/<slug>/<dep> origin/<ref>`, materialize `symlink_dirs` + `bind_files`, no `task setup`.
6. Write `.mship-workspace` marker at hub root.
7. Persist `worktrees` and `passive_repos` to state.

### `audit`

Existing checks against canonical checkouts unchanged. New issue codes for passive worktrees:

- `passive_drift` (warn) — passive worktree's HEAD ≠ `origin/<ref>` after fetch. Surfaces drift over a long-running task.
- `passive_dirty_worktree` (warn) — user manually edited a passive worktree (hook refuses commits, but `git stash`, manual `git add` etc. can still leave dirty trees).
- `passive_fetch_failed` (error) — fetch for a passive ref failed (network, auth). Same severity treatment as existing `fetch_failed` for affected repos.

Existing `extra_worktrees` check uses state.yaml as source of truth, so legacy per-repo worktrees from in-flight tasks aren't flagged.

### `sync`

- Canonical checkouts: existing semantics (strictly safe, fast-forward only).
- Passive worktrees: `git fetch origin <ref>`, then `git reset --hard origin/<ref>`. Safe because passive worktrees are mship-managed, detached HEAD, and hook-protected from commits — there is nothing the user could lose.
- Default: includes passive worktrees. `--no-passive` opts out.

### `close`

After existing close gates (recovery-path check, PR state routing) pass:
1. `git worktree remove` each entry in `task.worktrees`.
2. `rm -rf <workspace>/.worktrees/<slug>/`.

Passive worktrees come out alongside affected ones.

### `prune`

Two responsibilities:
1. Existing: orphaned worktrees in registered locations. Extends naturally to hub.
2. New: legacy `<repo>/.worktrees/feat/<slug>/` dirs from pre-hub tasks that have closed. Detected via `git worktree list` showing them as orphaned plumbing.

### `bind refresh`

Already covers `bind_files` + `symlink_dirs` (#71, #111). Extends to passive worktrees with no flag change — same per-repo iteration just covers more entries.

### `switch`

`mship switch <repo>` may target a passive repo. The handoff message warns:

```
⚠ Switched to `<repo>` (passive — read-only on `<ref>`).
  To edit, close this task and respawn with `--repos <repo>,...`
```

`mship test` and `mship phase` against a passive-active repo error out with the same guidance.

### `doctor`

Add a check: workspace root has `.worktrees` in its `.gitignore` if the root lives in a git repo. mship adds it on first spawn (existing behavior, new path); doctor verifies for sanity.

### `run` / `logs`

Unchanged. `run` operates on canonical checkouts per current semantics; healthchecks etc. unaffected by worktree topology.

## 4. Migration

### No data migration required

State.yaml stores absolute worktree paths. In-flight tasks remember exactly where they live and finish out under the original layout. Only spawns *after* upgrade use hub layout.

### Crossover state

Workspaces with both legacy and hub tasks are bounded and well-defined:

- **Old tasks** stay under `<repo>/.worktrees/feat/<slug>/`. `mship status`, `mship test`, `mship finish`, `mship close` all work — they read absolute paths from state.yaml.
- **New tasks** live under `<workspace>/.worktrees/<slug>/`. Same commands, same code paths.
- `mship audit` compares against state.yaml's recorded paths, so neither layout produces false positives.
- `mship prune` cleans up legacy `<repo>/.worktrees/` dirs as those tasks close. After the last legacy task closes, the directory is gone for good.

### Versioning

Bump to **0.2.0**. Behavior change in shape (worktree paths move) but not in API. No field removed, no flag changed — per-repo layout was implementation, not documented config.

### Release notes copy

> **mship 0.2.0 — Worktree layout change**
>
> New tasks spawned after upgrade live under `<workspace>/.worktrees/<slug>/<repo>/` (a per-task hub of sibling worktrees) instead of `<repo>/.worktrees/feat/<slug>/`. This makes cross-repo references (`cd ../sibling`, editable Python deps, etc.) work naturally inside a task.
>
> **In-flight tasks created before upgrade are unaffected** — they finish out under their original paths.
>
> **Watch out for:**
> - IDE workspace files or scripts pinned to old worktree paths. Update them when you create your next task.
> - The workspace root needs `.worktrees` in its `.gitignore` if the workspace itself lives in a git repo. mship adds it automatically on first spawn.
>
> **New behavior:** If a task touches `api` and `api.depends_on` includes `shared`, mship now materializes `shared` in the hub on its base/expected branch as a *passive worktree* — read-only, kept fresh from origin. To edit `shared`, include it in `--repos` like before.

## 5. Out of Scope

Called out explicitly so it's not a surprise later:

### `external_symlinks` (#110)

Deferred to a follow-up spec. Hub layout solves the bulk of #110's motivation:

| Original #110 use case | Now handled by |
|---|---|
| `cd ../sibling` in Taskfile | Hub layout |
| Editable Python deps to other repos in the task | Hub layout |
| Linking to non-affected sibling repos | Passive worktrees |
| Build caches at fixed absolute paths | Future `external_symlinks` |
| Secret/config mounts at fixed paths | Future `external_symlinks` |
| Workspace-internal non-repo dirs | Future `external_symlinks` |

The residual cases are real but small. Defer until we see what users actually struggle with under hub layout.

### Monorepo subdirectory off-by-one

Repos with `git_root: <parent>` and `path: <subdir>` live at `<workspace>/.worktrees/<slug>/<parent>/<subdir>/`. From there, `../sibling` resolves to `<workspace>/.worktrees/<slug>/<parent>/sibling`, not the actual sibling at `<workspace>/.worktrees/<slug>/sibling`. The Taskfile would need `../../sibling`.

This is the same off-by-one that exists in per-repo today (`web`'s worktree was at `<parent>/.worktrees/feat/<slug>/web/`, with the same problem). Hub doesn't fix it; doesn't worsen it. Tracking issue if it becomes painful.

## 6. Supported Configurations

| Workspace shape | Behavior |
|---|---|
| Single-repo workspace | Worktree at `<workspace>/.worktrees/<slug>/<repo>/`. One extra nesting level vs. per-repo, otherwise identical. No siblings, no passive worktrees ever materialized. |
| Multi-repo metarepo | Affected repos sit as siblings in the hub. Passive deps materialize alongside. `cd ../sibling` works by construction. **The win case.** |
| Monorepo with `git_root` subdirs | Parent worktree at `<workspace>/.worktrees/<slug>/<parent>/`. Subdir child at `<workspace>/.worktrees/<slug>/<parent>/<subdir>/`. Cross-repo references from a subdir have the off-by-one noted in §5. Same as today. |
| Repo with `path: .` (workspace IS canonical repo) | Worktree at `<workspace>/.worktrees/<slug>/<name>/` — inside that repo's own canonical checkout. `.gitignore` already handles this. |
| Repos at paths outside workspace (`path: ../external-repo`) | Canonical checkout wherever; worktree is still in the hub at `<workspace>/.worktrees/<slug>/<repo>/`. `git worktree add` accepts any absolute destination. |

## 7. Considered & Rejected Alternatives

### Opt-in `worktree_layout: per-repo | hub` setting (default per-repo)

Keeps both layouts forever as a documented choice. **Rejected** because:
- Permanent maintenance tax: every worktree-touching code path branches; tests double; doctor / audit / sync / prune carry the matrix.
- The setting is itself a footgun — users will pick `per-repo` (it sounds simpler) and then file bugs about cross-repo paths breaking, the exact thing hub was designed to fix. Shipping a known-broken-by-design option as a configurable choice is anti-product.
- Per-repo offers no segment of users a meaningfully better experience. Single-repo workspaces don't suffer from hub; multi-repo workspaces benefit from it.

### Default-flip with deprecation grace period

Default per-repo for one or two minor versions; deprecation warning when field is unset; flip default to hub. **Rejected** because:
- mship is pre-1.0. Now is exactly when clean breaks are appropriate.
- Adds two release cycles of carrying both layouts before the eventual flip — same maintenance tax as opt-in, just bounded.

### Hidden `MSHIP_LEGACY_LAYOUT=1` env var escape hatch

Considered as a hedge in case hub layout has a bug we don't catch in testing. **Rejected** because:
- Argument for testing, not for shipping a documented-but-undocumented escape hatch.
- If we discover a workspace that genuinely can't move, we'd add an escape hatch then with a real motivating example. Speculative escape hatches accrue cost without proven value.

### Auto-discover cross-repo references via per-ecosystem scanners

Scan Taskfiles, `pyproject.toml` editable deps, `package.json` workspace links, `docker-compose.yml` volume mounts, etc. to auto-materialize passive worktrees beyond `depends_on`. **Rejected** because:
- Each scanner is a maintenance liability (different ecosystems, frequent format churn) and will have false positives/negatives.
- Spawn becomes magic — user passes `--repos api`, gets worktrees they didn't request.
- Doesn't actually eliminate explicit declaration for non-repo external resources (caches, secrets) — adds magic on top of, not instead of, `external_symlinks`.
- Better answer: clear error message when a missing path is hit (`"add 'shared' to --repos or declare it in external_symlinks"`).
