# Multi-Repo Scaling Features Design Spec

## Overview

Four features that make mothership practical for large multi-repo workspaces (5+ repos, mixed tech stacks, slow test suites): repo filtering on execution commands, parallel execution within dependency tiers, repo tags for grouping, and dependency types (compile vs runtime).

## 1. `--repos` Filter on Execution Commands

### Commands Affected

`mship test`, `mship run`, `mship logs`

### Behavior

- `mship test --repos shared-swift,ios-app` — run tests only for those repos, in dependency order
- `mship test` (no flag) — all affected repos for current task, same as today
- `mship run --repos backend` — start just the backend
- No validation against the task's affected repos — you can test any repo in the workspace
- If `--repos` includes repos not in the workspace config, error: "Unknown repo: X"
- Repos in `--repos` are still topo-sorted before execution
- `--repos` and `--tag` can be combined — repos must match both filters

### Implementation

Add `repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names to filter")` to `test_cmd` and `run_cmd` in `cli/exec.py`. Parse the comma-separated string and pass to the executor instead of `task.affected_repos`.

For `logs`, the command already takes a single `service` argument — no change needed (it already filters to one repo). `--tag` is not applicable to `logs` since it targets a single service.

## 2. Parallel Execution Within Dependency Tiers

### How It Works

The executor groups repos into dependency tiers using the graph, then runs each tier in parallel using `concurrent.futures.ThreadPoolExecutor`.

For a metarepo workspace:
```
Tier 0: shared-swift, backend           (no deps — run in parallel)
Tier 1: ios-app, android-app, macos-app  (all deps satisfied — run in parallel)
```

### Default Behavior

Parallel within tiers is the default. No opt-in flag needed. Sequential execution within a tier has no correctness benefit — repos at the same tier are independent by definition.

### Fail-Fast Between Tiers

- All repos in tier 0 run concurrently
- Wait for all tier 0 repos to complete
- If any repo in tier 0 failed, stop — don't start tier 1
- `--all` flag: run all tiers regardless of failures, still parallel within each tier

### Implementation

New method on `DependencyGraph`:

```python
def topo_tiers(self, repos: list[str] | None = None) -> list[list[str]]:
    """Return repos grouped into dependency tiers.
    
    Each tier is a list of repos that can run concurrently.
    Tiers are ordered: tier N's deps are all in tiers 0..N-1.
    """
```

Example:
```python
graph.topo_tiers()
# [["shared-swift", "backend"], ["ios-app", "android-app", "macos-app"]]

graph.topo_tiers(repos=["shared-swift", "ios-app"])
# [["shared-swift"], ["ios-app"]]
```

`RepoExecutor.execute()` changes from iterating `topo_sort()` sequentially to iterating `topo_tiers()` with `ThreadPoolExecutor` per tier.

### Thread Safety

- Each repo execution is independent: different `cwd`, different shell process
- `StateManager.save()` uses atomic writes (temp file + rename)
- Test result updates write to different repo keys in `test_results` — no conflict at the dict level, but concurrent `load()` + `save()` cycles on the same file could race. Fix: collect results per tier, then batch-save after the tier completes (one save per tier, not per repo).

## 3. Repo Tags

### Config Syntax

```yaml
repos:
  shared-swift:
    path: ./repos/shared-swift
    type: library
    tags: [apple]
  ios-app:
    path: ./repos/ios-app
    type: service
    depends_on: [shared-swift]
    tags: [apple, mobile]
  android-app:
    path: ./repos/android-app
    type: service
    tags: [android, mobile]
```

### CLI

- `mship test --tag apple` — runs repos tagged `apple` (shared-swift, ios-app, macos-app)
- `mship test --tag mobile` — runs repos tagged `mobile` (ios-app, android-app)
- `mship test --tag apple --tag mobile` — runs repos that have `apple` OR `mobile` (union)
- `mship test --tag apple --repos ios-app` — repos must match both: ios-app has tag `apple`, so it runs. shared-swift has tag `apple` but isn't in `--repos`, so it's excluded.

### Behavior

- Tags are optional — repos without tags are matched by `--repos` or no filter
- Tags are freeform strings, no validation beyond non-empty
- `--tag` filter is available on `mship test` and `mship run`
- When filtering by tag, results are still topo-sorted for execution order

### Model Change

Add to `RepoConfig`:

```python
tags: list[str] = []
```

## 4. Dependency Types (Compile vs Runtime)

### Config Syntax

Backward compatible — plain strings default to `compile`:

```yaml
repos:
  shared-swift:
    path: ./repos/shared-swift
    type: library
  backend:
    path: ./repos/backend
    type: service
  ios-app:
    path: ./repos/ios-app
    type: service
    depends_on:
      - repo: shared-swift
        type: compile
      - repo: backend
        type: runtime
  android-app:
    path: ./repos/android-app
    type: service
    depends_on:
      - repo: backend
        type: runtime
```

Plain string shorthand: `depends_on: [shared-swift]` is equivalent to `depends_on: [{repo: shared-swift, type: compile}]`.

### Model Change

New model:

```python
class Dependency(BaseModel):
    repo: str
    type: Literal["compile", "runtime"] = "compile"
```

`RepoConfig.depends_on` changes from `list[str]` to `list[str | Dependency]` with a Pydantic validator that normalizes strings to `Dependency(repo=name, type="compile")`.

### Graph Behavior

Both `compile` and `runtime` deps affect execution order and tier placement. A runtime dep means "this must complete before I start" — same tier ordering as compile deps. The distinction is only in the env vars.

### Env Var Changes

For each dependency with a worktree, the executor passes:

```
UPSTREAM_SHARED_SWIFT=/path/to/worktree
UPSTREAM_SHARED_SWIFT_TYPE=compile
UPSTREAM_BACKEND=/path/to/worktree
UPSTREAM_BACKEND_TYPE=runtime
```

The `_TYPE` env var tells the Taskfile/Dagger how to use the upstream path:
- `compile`: mount and build against the source
- `runtime`: build, start as a service, test against it

### Backward Compatibility

- Existing `depends_on: [shared]` configs continue to work (normalized to compile type)
- Existing `UPSTREAM_*` env vars continue to work (new `_TYPE` vars are additive)
- Existing graph operations (`topo_sort`, `dependents`, `dependencies`) work unchanged — they just see all deps regardless of type

## Files Changed/Created

| File | Change | Purpose |
|------|--------|---------|
| `src/mship/core/config.py` | Modify | Add `tags` to RepoConfig, add `Dependency` model, update `depends_on` type + validator |
| `src/mship/core/graph.py` | Modify | Add `topo_tiers()` method, update internals to work with `Dependency` objects |
| `src/mship/core/executor.py` | Modify | Parallel tier execution, `_TYPE` env vars, batch test result saves |
| `src/mship/cli/exec.py` | Modify | Add `--repos` and `--tag` flags to test and run commands |
| `tests/core/test_config.py` | Modify | Test tags, Dependency model, backward compat |
| `tests/core/test_graph.py` | Modify | Test topo_tiers |
| `tests/core/test_executor.py` | Modify | Test parallel execution, tag/repo filtering, _TYPE env vars |
| `tests/cli/test_exec.py` | Modify | Test --repos and --tag CLI flags |
