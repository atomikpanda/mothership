# Mothership v1 Design Spec

## Overview

Mothership (`mship`) is a CLI tool that AI coding agents call via bash for phase-based workflow orchestration, worktree management, and task execution. It works for single-repo projects (phase tracking, worktree isolation, structured execution) and scales to multi-repo workspaces (dependency ordering, coordinated worktrees, cross-repo execution). It sits above superpowers (per-repo methodology) and go-task (per-repo execution).

**v1 scope:** Config/awareness, worktrees (single and multi-repo), orchestrated execution, phases.

**Out of scope for v1:** Issue intake (`mship issue`), zellij integration, automatic impact analysis, multi-agent orchestration, MCP server.

## Stack

- **Language:** Python 3.14
- **CLI framework:** Typer
- **Validation/models:** Pydantic v2
- **Terminal output:** Rich
- **Interactive prompts:** InquirerPy
- **Dependency injection:** dependency-injector
- **Packaging/tooling:** uv
- **Distribution:** `uv tool install`

## Architecture

Layered — thin CLI shell delegates to a core library. Core is CLI-independent so it can be reused by a v2 MCP server.

```
CLI layer (Typer + Rich + InquirerPy)
    ↓ calls
Core layer (DI-wired services, pure logic)
    ↓ calls
Util layer (git operations, shell execution, slugification)
```

### Project Structure

```
mothership/
├── pyproject.toml
├── src/
│   └── mship/
│       ├── __init__.py
│       ├── cli/
│       │   ├── __init__.py       # Typer app, entry point
│       │   ├── status.py         # mship status, graph
│       │   ├── phase.py          # mship phase
│       │   ├── worktree.py       # mship spawn, finish, abort, worktrees
│       │   ├── exec.py           # mship test, run, logs
│       │   └── output.py         # TTY detection, Rich vs JSON formatting
│       ├── core/
│       │   ├── __init__.py
│       │   ├── config.py         # Pydantic models for mothership.yaml
│       │   ├── state.py          # Pydantic models for state.yaml, read/write
│       │   ├── worktree.py       # Cross-repo worktree orchestration
│       │   ├── executor.py       # Task execution across repos
│       │   ├── phase.py          # Phase transitions, soft gate logic
│       │   └── graph.py          # Dependency graph, topological sort
│       ├── util/
│       │   ├── __init__.py
│       │   ├── git.py            # Git operations
│       │   ├── shell.py          # Subprocess execution, env_runner wrapping
│       │   └── slug.py           # Task name to branch slug
│       └── container.py          # DI container
├── tests/
└── docs/
```

## Configuration (`mothership.yaml`)

Located at workspace root. Declares repos, dependencies, and execution config.

### Discovery

`mship` searches for `mothership.yaml` starting from the current working directory, walking up parent directories until found (same pattern as `.git`, `Taskfile.yml`). The directory containing `mothership.yaml` is the workspace root. All repo `path` values are resolved relative to this root. If not found, `mship` errors with a clear message.

### Schema

```yaml
workspace: my-platform

# Optional workspace-level defaults
env_runner: "dotenvx run --"       # wraps all task execution
branch_pattern: "feat/{slug}"      # {slug} replaced at spawn time

repos:
  shared:
    path: ./shared
    type: library                  # "library" | "service"
    depends_on: []
    env_runner: "op run --"        # overrides workspace default
    tasks:                         # override canonical task names
      test: unit                   # mship test → task unit

  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]

  api-gateway:
    path: ./api-gateway
    type: service
    depends_on: [shared, auth-service]
```

### Pydantic Models

```python
class RepoConfig(BaseModel):
    path: Path
    type: Literal["library", "service"]
    depends_on: list[str] = []
    env_runner: str | None = None
    tasks: dict[str, str] = {}

class WorkspaceConfig(BaseModel):
    workspace: str
    env_runner: str | None = None
    branch_pattern: str = "feat/{slug}"
    repos: dict[str, RepoConfig]
```

### Validation Rules

- All `depends_on` entries must reference existing repo keys.
- No circular dependencies (topological sort must succeed).
- All `path` values must point to existing directories containing a `Taskfile.yml`.
- Pydantic v2 model validators handle all validation at config load time.

## State (`.mothership/state.yaml`)

Managed by mothership, gitignored. Tracks active tasks, worktrees, phases, and test results.

### Schema

```yaml
current_task: add-labels-to-tasks

tasks:
  add-labels-to-tasks:
    slug: add-labels-to-tasks
    description: "Add labels to tasks"
    phase: dev
    created_at: "2026-04-10T14:30:00Z"
    affected_repos: [shared, auth-service]
    worktrees:
      shared: /home/user/dev/shared/.worktrees/feat/add-labels-to-tasks
      auth-service: /home/user/dev/auth-service/.worktrees/feat/add-labels-to-tasks
    branch: feat/add-labels-to-tasks
    test_results:
      shared: {status: pass, at: "2026-04-10T15:00:00Z"}
      auth-service: {status: fail, at: "2026-04-10T15:01:00Z"}
```

### Pydantic Models

```python
class TestResult(BaseModel):
    status: Literal["pass", "fail", "skip"]
    at: datetime

class Task(BaseModel):
    slug: str
    description: str
    phase: Literal["plan", "dev", "review", "run"]
    created_at: datetime
    affected_repos: list[str]
    worktrees: dict[str, Path] = {}
    branch: str
    test_results: dict[str, TestResult] = {}

class WorkspaceState(BaseModel):
    current_task: str | None = None
    tasks: dict[str, Task] = {}
```

### State Management

- `.mothership/` directory gitignored.
- Atomic writes: write to temp file, rename into place.
- File locking for safety.
- `StateManager` service handles all read/write.
- Single active task for v1. `tasks` is a dict to support concurrent tasks in v2 (multi-agent).

## Dependency Graph

Built from `depends_on` declarations in `mothership.yaml`. Used for execution order, merge order, and affected repo expansion.

### Operations

- **`topo_sort()`** — returns repos in dependency order (libraries first, then dependents). Used by `mship test`, `mship run`, `mship spawn`, and `mship finish`.
- **`dependents(repo)`** — returns all transitive downstream repos. If `shared` changes, returns `[auth-service, api-gateway]`.
- **Cycle detection** — performed at config validation time. Pydantic validator calls `topo_sort()` and catches cycle errors.

### Implementation

- Simple adjacency list in `graph.py`. No external graph library needed for v1.
- Topological sort via Kahn's algorithm or DFS-based.

## Executor

Runs `task <name>` across repos in dependency order, wrapping with env_runner.

### Command Construction

```
cd <repo_path> && [env_runner] task <task_name>
```

The effective env_runner is resolved per repo: repo override > workspace default > none (plain `task` execution).

Task name resolution: check repo's `tasks` mapping for an override, fall back to canonical name.

### Error Propagation

- **Default (fail-fast):** Stop on first repo failure. If `shared` tests fail, don't run downstream repos.
- **`--all` flag:** Run every affected repo regardless of failures, then present aggregate summary.
- Missing tasks are skipped gracefully with a warning (e.g., a library without a `logs` task).

### Canonical Task Names

Default contract: `test`, `run`, `lint`, `logs`, `setup`. Configurable per repo via the `tasks` mapping in `mothership.yaml`.

## Phase Management

Four fixed phases: `plan`, `dev`, `review`, `run`.

### Transitions

`mship phase <target>` transitions the current task to the target phase. Any direction is allowed (including backwards). Forward transitions trigger soft gate checks.

### Soft Gates

Soft gates warn but never block. The phase always transitions.

| Target | Warning condition | Message |
|--------|-------------------|---------|
| `dev` | No spec/plan file found in workspace | "No spec found — consider writing one before developing" |
| `review` | Any affected repo has failing or no test results | "Tests not passing in: auth-service — consider fixing before review" |
| `run` | Uncommitted changes in any affected repo worktree | "Uncommitted changes in: shared — consider committing before run" |
| `plan` | *(no gate)* | — |

### Implementation

- `phase.py` in core has a `transition(task, target_phase)` method.
- Returns a `PhaseTransition` result with the new phase + list of warnings.
- CLI layer formats warnings via Rich (yellow) or includes them in JSON output.

### Phase Semantics (v1)

For v1, the phase is primarily metadata that `mship status` reports. No layout switching or tool gating. The future superpowers skill will read the phase to guide agent behavior.

## Cross-Repo Worktree Orchestration

### `mship spawn "<description>" [--repos repo1,repo2]`

**Step 1: Slugify.** `"add labels to tasks"` becomes `add-labels-to-tasks`. Apply branch pattern: `feat/add-labels-to-tasks`.

**Step 2: Resolve affected repos.** Explicit `--repos` flag, or all repos if omitted. No automatic impact analysis in v1.

**Step 3: Create worktrees.** In dependency order:
1. `git worktree add <repo_path>/.worktrees/<branch> -b <branch>`
2. Verify `.worktrees` is gitignored in each repo; add to `.gitignore` if not.
3. Run `task setup` in each worktree if the task exists (skip gracefully if not).

**Step 4: Record state.** Write task entry to `state.yaml` with worktree paths, affected repos, phase set to `plan`.

### `mship finish`

**Step 1: Soft gate.** Warn if any affected repos have failing tests.

**Step 2: Create PRs in dependency order.** For each repo (topo-sorted):
- Prompt for action: create PR, merge to main, keep branch, wait (via InquirerPy when TTY, JSON output when not).
- Check if upstream dependency PRs are merged. If not, warn.
- Create PR via `gh pr create` with coordination block in description.

**Step 3: Update PRs with cross-references.** After all PRs exist, update each PR body so the coordination table has complete links.

**Step 4: Clean up.** Remove worktrees via `git worktree remove`, remove task from state.

### PR Coordination Block

Appended to each PR description automatically:

```markdown
## Cross-repo coordination (mothership)

This PR is part of a coordinated change: `add-labels-to-tasks`

| # | Repo | PR | Merge order |
|---|------|----|-------------|
| 1 | shared | org/shared#18 | merge first |
| 2 | auth-service | org/auth-service#42 | this PR |

Merge in order — auth-service depends on shared.
```

### `mship abort`

- Prompts for confirmation via InquirerPy (skipped when not a TTY).
- Removes all worktrees for the current task (`git worktree remove --force`).
- Deletes branches (`git branch -D`).
- Removes task from state.

### `mship worktrees`

Lists active worktrees grouped by task, with repo, path, and branch.

## CLI Commands (v1)

```bash
# Workspace awareness
mship status                          # current phase, active task, worktrees, test results
mship graph                           # show repo dependency graph

# Phase management
mship phase plan                      # transition to plan phase
mship phase dev                       # transition to dev phase
mship phase review                    # transition to review phase
mship phase run                       # transition to run phase

# Cross-repo worktrees
mship spawn "add labels to tasks"     # create coordinated worktrees (all repos)
mship spawn "fix auth" --repos shared,auth-service  # explicit repo list
mship finish                          # PR/merge/cleanup in dependency order
mship abort                           # discard worktrees, abandon task
mship worktrees                       # list active worktrees by task

# Execution (delegates to task per repo)
mship test                            # run tests in dependency order (fail-fast)
mship test --all                      # run all, aggregate results
mship run                             # start services in dependency order
mship logs <service>                  # tail logs for a service
```

## CLI Output

- **TTY detected (`sys.stdout.isatty()`):** Rich-formatted tables, colored warnings, interactive prompts via InquirerPy.
- **Not a TTY (piped/agent):** JSON output. Structured for machine parsing. No interactive prompts — use defaults or error.

Single code path: output functions check TTY once and delegate to Rich or JSON serialization.

## Dependency Injection

```python
class Container(containers.DeclarativeContainer):
    config = providers.Singleton(
        ConfigLoader.load,
        path=providers.Dependency(),
    )

    state_manager = providers.Singleton(
        StateManager,
        state_dir=providers.Dependency(),
    )

    graph = providers.Factory(
        DependencyGraph,
        config=config,
    )

    executor = providers.Factory(
        RepoExecutor,
        config=config,
        graph=graph,
        state_manager=state_manager,
    )

    worktree_manager = providers.Factory(
        WorktreeManager,
        config=config,
        graph=graph,
        state_manager=state_manager,
    )

    phase_manager = providers.Factory(
        PhaseManager,
        state_manager=state_manager,
    )
```

### Testing Strategy

- **Unit tests:** Override providers with mocks. `container.state_manager.override(providers.Object(mock_state))`. Core logic tested without filesystem or git.
- **Integration tests:** Wire real providers, use temp directories with real git repos.
- **CLI tests:** Typer's `CliRunner` with container overrides.

### Key Interfaces to Mock

- `ShellRunner` — wraps subprocess calls (git, task). Easy to stub.
- `StateManager` — read/write state.yaml. Swap for in-memory dict in tests.
- `ConfigLoader` — parse mothership.yaml. Swap for fixture configs.

## v2 Roadmap (Out of Scope, Informing v1 Decisions)

These are not v1 features, but v1 design decisions account for them:

- **Multi-agent orchestration** — confirmed priority. v1 state format (dict of tasks) and machine-parseable CLI output support this without redesign.
- **MCP server** — core library is CLI-independent, callable directly.
- **Issue intake** (`mship issue gh:...`) — GitHub issue to phase pipeline.
- **Zellij integration** — phase-based terminal layouts.
- **Curated context** (`mship context`) — expanded repo context with key interfaces and API surface.
- **Automatic impact analysis** — infer affected repos from imports/OpenAPI/proto.
- **Superpowers skill** — `working-with-mothership` skill that teaches agents to use `mship`.
