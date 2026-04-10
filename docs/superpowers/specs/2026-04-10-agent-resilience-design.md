# Agent Resilience Features Design Spec

## Overview

Four features that make mothership robust for autonomous agent workflows: blocked state tracking, context recovery via task logs, CI/CD handoff manifests, and worktree garbage collection. All are additive — no breaking changes to existing behavior.

## 1. Blocked State Overlay

### Problem

When an agent is waiting on human input, an API key, or a long-running process, it has no way to park the current task and record why it's paused. The phase model (plan/dev/review/run) describes what kind of work is happening, not whether work is possible.

### Design

`blocked` is an overlay on the current phase, not a 5th phase. The task's phase stays where it was (e.g., `dev`), and two new fields track the blocked state.

#### State Model Changes

Add to the `Task` model in `core/state.py`:

```python
class Task(BaseModel):
    # ... existing fields ...
    blocked_reason: str | None = None
    blocked_at: datetime | None = None
```

#### CLI Commands

**`mship block "reason"`** — sets `blocked_reason` and `blocked_at` on the current task. Errors if no active task. Auto-logs the block event to the task log.

**`mship unblock`** — clears `blocked_reason` and `blocked_at`. Errors if the task isn't blocked. Auto-logs the unblock event.

#### Status Display

TTY output:
```
Task: add-labels
Phase: dev (BLOCKED: waiting on API key)
Blocked since: 2026-04-10T15:00:00Z
```

JSON output includes `blocked_reason` and `blocked_at` fields directly in the task object.

#### Behavior

- No phase transition occurs — the phase stays where it was
- `mship phase <target>` still works while blocked (implicitly unblocks and auto-logs "Unblocked (phase transition to <target>)")
- `mship test`, `mship run`, etc. still work while blocked (mothership doesn't enforce the block — it's informational for the agent)

## 2. Context Recovery (Task Log)

### Problem

When an agent's context window is wiped (crash, token limit, new session), `mship status` tells it the phase and test results, but not the narrative of what was happening. The agent loses its working memory.

### Design

A per-task append-only markdown log in `.mothership/logs/<task-slug>.md`. Agents write breadcrumbs as they work. On recovery, they read the log to regain context.

#### Storage

`.mothership/logs/<task-slug>.md`:

```markdown
# Task Log: add-labels-to-tasks

## 2026-04-10T15:00:00Z
Task spawned. Repos: shared, auth-service. Branch: feat/add-labels-to-tasks

## 2026-04-10T15:05:00Z
Phase transition: plan → dev

## 2026-04-10T15:10:00Z
Refactored the auth controller, tests passing. Database schema still needs updating.

## 2026-04-10T15:30:00Z
Updated schema migration. Running integration tests now.
```

#### CLI Commands

**`mship log "message"`** — appends a timestamped entry to the current task's log. Errors if no active task.

**`mship log`** (no args) — reads and displays the full log for the current task. TTY: rendered markdown. Non-TTY: JSON array of entries with `timestamp` and `message` fields.

**`mship log --last N`** — shows only the last N entries.

#### Auto-Logging

These lifecycle events are logged automatically:
- `mship spawn` — "Task spawned. Repos: X, Y. Branch: feat/slug"
- `mship phase <target>` — "Phase transition: old → new"
- `mship block "reason"` — "Blocked: reason"
- `mship unblock` — "Unblocked"

#### Lifecycle

- Log file is created by `mship spawn`
- Log file persists after `mship abort` and `mship finish` — it's history, not ephemeral state
- `.mothership/logs/` is gitignored alongside the rest of `.mothership/`

#### Implementation

New `LogManager` service in `core/log.py`:

```python
@dataclass
class LogEntry:
    timestamp: datetime
    message: str

class LogManager:
    def __init__(self, logs_dir: Path) -> None: ...
    def append(self, task_slug: str, message: str) -> None: ...
    def read(self, task_slug: str, last: int | None = None) -> list[LogEntry]: ...
    def create(self, task_slug: str) -> None: ...
```

Added to the DI container as a `Singleton` provider with `logs_dir` derived from the state directory.

New CLI module `cli/log.py` with the `log` command registered in `cli/__init__.py`.

#### Agent Recovery Flow

```bash
mship status    # what task, what phase, what's blocked
mship log       # what was I doing, what's the narrative
```

Two commands, full context recovery.

## 3. CI/CD Handoff Manifest

### Problem

`mship finish` shows the PR merge order, but relies on the agent or human to execute it correctly. In complex environments, a PR merged out of order breaks main. CI needs a structured description of what to merge and in what order.

### Design

`mship finish --handoff` generates a YAML manifest that any CI system can consume. Mothership provides the data; CI integration is the user's responsibility.

#### Output

`.mothership/handoffs/<task-slug>.yaml`:

```yaml
task: add-labels-to-tasks
branch: feat/add-labels-to-tasks
generated_at: "2026-04-10T16:00:00Z"

merge_order:
  - order: 1
    repo: shared
    path: ./shared
    branch: feat/add-labels-to-tasks
    depends_on: []
    pr: null
  - order: 2
    repo: auth-service
    path: ./auth-service
    branch: feat/add-labels-to-tasks
    depends_on: [shared]
    pr: null
```

#### Pydantic Model

```python
class MergeOrderEntry(BaseModel):
    order: int
    repo: str
    path: Path
    branch: str
    depends_on: list[str]
    pr: str | None = None

class HandoffManifest(BaseModel):
    task: str
    branch: str
    generated_at: datetime
    merge_order: list[MergeOrderEntry]
```

#### Behavior

- `mship finish --handoff` writes the manifest, prints its path, and exits. Does NOT clean up worktrees or delete state.
- `mship finish` (without `--handoff`) behaves as today — shows merge order, warns about manual PR creation.
- The manifest is a point-in-time snapshot of the dependency graph and branch state.
- `.mothership/handoffs/` is gitignored.

#### What It Is NOT

- Not CI config generation — no `.github/workflows/` files
- Not PR creation — that's future work
- Not a merge executor — describes what to merge, not how

## 4. Worktree Garbage Collection (`mship prune`)

### Problem

Agents crash, hit token limits, or abort uncleanly. This leaves orphaned worktrees on disk that eat space and pollute `git worktree list`. There's no way to find and clean them up.

### Design

`mship prune` cross-references the filesystem against state to find orphans, then cleans them up.

#### Detection

Two sources of truth:
1. **Filesystem** — scan each repo's `.worktrees/` directory for actual worktree directories
2. **State** — read `.mothership/state.yaml` for tracked worktrees

Two orphan categories:
- **On-disk, not in state** — worktree directory exists but mothership doesn't track it (agent crashed mid-spawn, or state was corrupted)
- **In state, not on disk** — state references a worktree path that no longer exists (manually deleted or lost)

#### CLI

**`mship prune`** (default: dry-run) — lists orphans without removing them.

```
Orphaned worktrees found:
  shared/.worktrees/feat/old-experiment (not in state)
  State entry "stale-task" references missing worktree at /tmp/gone

Run `mship prune --force` to clean up.
```

**`mship prune --force`** — actually removes orphans.

```
Removed: shared/.worktrees/feat/old-experiment
Cleaned state entry: stale-task
Pruned 2 items.
```

Non-TTY: JSON output with `orphans` array and `pruned` boolean.

#### Cleanup Actions

- **On-disk orphans:** `git worktree remove --force`, then `git branch -D` if the branch exists
- **State-only orphans:** remove the task entry from `state.yaml`, clear `current_task` if it pointed to the removed entry
- **Stale git tracking:** run `git worktree prune` per repo to clean git's internal worktree tracking

#### Implementation

New `PruneManager` service in `core/prune.py`:

```python
@dataclass
class OrphanedWorktree:
    repo: str
    path: Path
    reason: str  # "not_in_state" | "not_on_disk"

class PruneManager:
    def __init__(self, config: WorkspaceConfig, state_manager: StateManager, git: GitRunner) -> None: ...
    def scan(self) -> list[OrphanedWorktree]: ...
    def prune(self, orphans: list[OrphanedWorktree]) -> int: ...
```

Added to the DI container. New CLI module `cli/prune.py`.

#### No Changes to `abort`

Current `abort` already uses `git worktree remove --force` and `git branch -D`. It's aggressive enough. `prune` covers the gap `abort` can't — worktrees that were never properly tracked in state.

## New CLI Commands Summary

```bash
# Blocked state
mship block "reason"           # mark current task as blocked
mship unblock                  # clear blocked state

# Task log
mship log "message"            # append entry to current task's log
mship log                      # read full task log
mship log --last N             # read last N entries

# Handoff
mship finish --handoff         # generate CI handoff manifest

# Prune
mship prune                    # dry-run: list orphaned worktrees
mship prune --force            # remove orphaned worktrees
```

## Files Changed/Created

| File | Change | Purpose |
|------|--------|---------|
| `src/mship/core/state.py` | Modify | Add `blocked_reason`, `blocked_at` to Task model |
| `src/mship/core/log.py` | Create | LogManager for per-task append-only logs |
| `src/mship/core/prune.py` | Create | PruneManager for orphan detection and cleanup |
| `src/mship/core/handoff.py` | Create | HandoffManifest model and generation |
| `src/mship/cli/block.py` | Create | `mship block` and `mship unblock` commands |
| `src/mship/cli/log.py` | Create | `mship log` command |
| `src/mship/cli/prune.py` | Create | `mship prune` command |
| `src/mship/cli/worktree.py` | Modify | Add `--handoff` flag to `finish` |
| `src/mship/cli/status.py` | Modify | Show blocked overlay in status output |
| `src/mship/cli/__init__.py` | Modify | Register new command modules |
| `src/mship/container.py` | Modify | Add LogManager, PruneManager providers |
| `tests/core/test_log.py` | Create | LogManager tests |
| `tests/core/test_prune.py` | Create | PruneManager tests |
| `tests/core/test_handoff.py` | Create | HandoffManifest tests |
| `tests/cli/test_block.py` | Create | block/unblock CLI tests |
| `tests/cli/test_log.py` | Create | log CLI tests |
| `tests/cli/test_prune.py` | Create | prune CLI tests |
