# Mothership (`mship`)

Phase-based workflow engine for multi-repo AI development.

## The Problem

AI coding agents (Claude Code, Codex, Gemini CLI) are powerful within a single repo. Tools like [superpowers](https://github.com/obra/superpowers) handle methodology — brainstorming, planning, TDD, code review — and [go-task](https://taskfile.dev) handles execution. Both work brilliantly inside one repository.

But real systems aren't one repo. When a feature touches `auth-service` AND `api-gateway` AND a shared library, the agent has no way to:

- **Know which repos are affected** by a given change
- **Understand the dependency order** between repos (shared must build before auth-service)
- **Create coordinated worktrees** across multiple repos for a single task
- **Run tests in the right order** — if shared breaks, don't bother testing downstream repos
- **Track what phase of work you're in** across the whole effort
- **Merge PRs in the right order** so CI doesn't break on main

Every agent today works in isolation. Mothership gives them the cross-repo awareness they're missing.

## How It Works

Mothership is a CLI tool that agents call via bash. It's not an agent itself — it's infrastructure that agents use.

```
Agent (Claude Code, Codex, etc.)
    ↓ calls
Mothership (cross-repo coordination)
    ↓ delegates to
go-task (per-repo execution)
```

### 1. Declare your workspace

```yaml
# mothership.yaml
workspace: my-platform

repos:
  shared:
    path: ./shared
    type: library
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
  api-gateway:
    path: ./api-gateway
    type: service
    depends_on: [shared, auth-service]
```

Each repo has its own `Taskfile.yml` with standard task names (`test`, `run`, `lint`, `logs`, `setup`). Mothership orchestrates across repos by calling `task` in the right directory, in the right order.

### 2. Spawn coordinated worktrees

```bash
$ mship spawn "add labels to tasks" --repos shared,auth-service
Spawned task: add-labels-to-tasks
  Branch: feat/add-labels-to-tasks
  Phase: plan
  Repos: shared, auth-service
  shared: /home/user/dev/shared/.worktrees/feat/add-labels-to-tasks
  auth-service: /home/user/dev/auth-service/.worktrees/feat/add-labels-to-tasks
```

One command creates worktrees with matching branch names across every affected repo. The agent works in these worktrees, and cleanup is one command away.

### 3. Track phases

```bash
$ mship phase dev
WARNING: No spec found — consider writing one before developing
Phase: dev

$ mship phase review
WARNING: Tests not run in: auth-service — consider running tests before review
Phase: review
```

Four phases — `plan`, `dev`, `review`, `run` — with soft gates that warn (but don't block) when preconditions aren't met. The agent knows what phase it's in and gets contextual guidance.

### 4. Execute across repos

```bash
$ mship test
shared: pass
auth-service: pass

$ mship test --all    # run everything even if one fails
shared: fail
auth-service: pass    # ran anyway, might have its own issues
```

Tests run in dependency order. If `shared` fails, downstream repos are skipped by default (they'd fail anyway). Pass `--all` to get the full picture.

### 5. See where things stand

```bash
$ mship status
Task: add-labels-to-tasks
Phase: dev
Branch: feat/add-labels-to-tasks
Repos: shared, auth-service
Tests:
  shared: pass
  auth-service: fail

$ mship graph
  shared (library)
  auth-service (service) -> [shared]
  api-gateway (service) -> [shared, auth-service]
```

## Why Agents Need This

### Without Mothership

The agent changes `shared`, runs its tests (pass), moves to `auth-service`, changes it, runs tests (fail). It has no idea if the failure is because of the `shared` change or a bug in `auth-service`. It doesn't know it should have tested `shared` first. It creates PRs in random order and CI breaks on main because `auth-service` was merged before `shared`.

### With Mothership

The agent calls `mship spawn`, gets coordinated worktrees. It calls `mship test`, which runs `shared` first (dependency order) and stops if it fails. It calls `mship finish` and gets told the merge order. The cross-repo PR descriptions tell reviewers which PR to merge first.

The agent doesn't need to understand the repo graph. Mothership does.

## CLI Reference

```bash
# Workspace awareness
mship status                    # current phase, task, worktrees, test results
mship graph                     # show repo dependency graph

# Phase management
mship phase plan|dev|review|run # transition with soft gate warnings

# Cross-repo worktrees
mship spawn "description"      # create coordinated worktrees (all repos)
mship spawn "desc" --repos a,b # explicit repo list
mship worktrees                # list active worktrees
mship abort [--yes]            # discard worktrees, abandon task
mship finish                   # show merge order for PRs

# Execution (delegates to go-task per repo)
mship test [--all]             # run tests in dependency order
mship run                     # start services in dependency order
mship logs <service>           # tail logs for a service
```

## Configuration

### `mothership.yaml`

```yaml
workspace: my-platform

# Optional: wraps all task execution with a secret manager
env_runner: "dotenvx run --"

# Optional: branch naming pattern ({slug} is replaced)
branch_pattern: "feat/{slug}"

repos:
  shared:
    path: ./shared
    type: library            # "library" or "service"
    depends_on: []
    env_runner: "op run --"  # per-repo override
    tasks:
      test: unit             # override canonical task name
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
```

### Secret Management

Mothership doesn't manage secrets. It delegates to your secret manager via `env_runner`:

| Tool | Config value |
|------|-------------|
| dotenvx | `dotenvx run --` |
| Doppler | `doppler run --` |
| 1Password CLI | `op run --` |
| Infisical | `infisical run --` |
| None | omit `env_runner` |

### Taskfile Contract

Each repo needs a `Taskfile.yml` with standard task names. Mothership calls `task <name>` in each repo. Override names per repo in the `tasks` mapping.

Default tasks: `test`, `run`, `lint`, `logs`, `setup`. Missing tasks are skipped gracefully.

## Output

Mothership auto-detects whether it's talking to a human or an agent:

- **Terminal (TTY):** Rich-formatted tables, colored warnings, interactive prompts
- **Piped/agent (not TTY):** JSON output, no interactive prompts

Agents get structured JSON they can parse. Humans get readable output. Same command, no flags needed.

## Installation

```bash
uv tool install .
```

Requires Python 3.14+.

## What Mothership Is Not

- **Not a replacement for superpowers** — methodology stays with superpowers per repo
- **Not a task runner** — delegates to go-task
- **Not an AI agent** — it's a tool agents call
- **Not a monorepo tool** — works with separate repos in a shared workspace

## Stack

Python 3.14, Typer, Pydantic v2, Rich, InquirerPy, dependency-injector

## License

MIT
