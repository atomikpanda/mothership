# Mothership (`mship`)

Phase-based workflow engine for agentic development.

## The Problem

AI coding agents (Claude Code, Codex, Gemini CLI) are fast and capable, but they lack workflow structure. An agent can write code, run tests, and commit — but it doesn't know what *phase* of work it's in, whether it should be planning or coding, whether tests should pass before it moves to review, or how to coordinate worktrees for isolated feature work.

For single-repo projects, agents need:

- **Phase tracking** — know whether you're planning, developing, reviewing, or running
- **Soft gates** — warnings when you skip steps (no spec before coding, no tests before review)
- **Worktree management** — isolated branches for each task, clean setup and teardown
- **Structured task execution** — run tests, start services, tail logs through a consistent interface

For multi-repo projects, agents also need:

- **Dependency awareness** — which repos depend on which, what order to build/test/merge
- **Coordinated worktrees** — matching branches across multiple repos for a single task
- **Cross-repo execution** — run tests across repos in dependency order, fail-fast or run all
- **Merge ordering** — know which PR to merge first so CI doesn't break on main

Mothership handles both. Start with one repo, expand to many when your system grows.

## How It Works

Mothership is a CLI tool that agents call via bash. It's not an agent itself — it's infrastructure that agents use.

```
Agent (Claude Code, Codex, etc.)
    ↓ calls
Mothership (workflow orchestration)
    ↓ delegates to
go-task (per-repo execution)
```

### 1. Declare your workspace

A workspace can be a single repo or many:

```yaml
# mothership.yaml — single repo
workspace: my-app

repos:
  my-app:
    path: .
    type: service
```

```yaml
# mothership.yaml — multi-repo
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

Each repo has its own `Taskfile.yml` with standard task names (`test`, `run`, `lint`, `logs`, `setup`). Mothership calls `task` in the right directory, in the right order.

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

### Single repo: discipline without overhead

Without mothership, the agent jumps straight to coding. There's no spec, no plan, no phase awareness. It writes code, maybe runs tests, commits — and you end up reviewing a diff with no structure behind it.

With mothership, `mship spawn "add user avatars"` creates an isolated worktree. `mship phase dev` warns if there's no spec. `mship phase review` warns if tests haven't been run. The agent gets guardrails that keep it on track without slowing it down.

### Multi-repo: coordination without chaos

Without mothership, the agent changes `shared`, runs its tests (pass), moves to `auth-service`, changes it, runs tests (fail). It has no idea if the failure is because of the `shared` change or a bug in `auth-service`. It creates PRs in random order and CI breaks on main.

With mothership, `mship test` runs `shared` first (dependency order) and stops if it fails. `mship finish` tells you the merge order. The agent doesn't need to understand the repo graph — mothership does.

## CLI Reference

```bash
# Workspace awareness
mship status                    # current phase, task, worktrees, test results
mship graph                     # show repo dependency graph

# Phase management
mship phase plan|dev|review|run # transition with soft gate warnings

# Worktree management
mship spawn "description"      # create worktrees for a new task
mship spawn "desc" --repos a,b # explicit repo list (multi-repo)
mship worktrees                # list active worktrees
mship abort [--yes]            # discard worktrees, abandon task
mship finish                   # show merge order for PRs

# Execution (delegates to go-task per repo)
mship test [--all]             # run tests (in dependency order for multi-repo)
mship run                     # start services
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
uv tool install git+https://github.com/atomikpanda/mothership.git
```

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

Optional: install [go-task](https://taskfile.dev) (for task execution) and [gh](https://cli.github.com) (for PR creation).

## Getting Started

### 1. Install mothership

```bash
uv tool install git+https://github.com/atomikpanda/mothership.git
```

### 2. Initialize your workspace

**Interactive (for humans):**
```bash
cd ~/my-project
mship init
```

The wizard walks you through repo detection, types, dependencies, and optional Taskfile scaffolding.

**Non-interactive (for agents):**
```bash
# Single repo
mship init --name my-app --repo ./.:service

# Multi-repo
mship init --name my-platform \
  --repo ./shared:library \
  --repo ./auth-service:service:shared \
  --repo ./api-gateway:service:shared,auth-service
```

### 3. Verify your setup

```bash
mship doctor    # Check everything is configured correctly
mship status    # Should show "No active task"
mship graph     # Shows your repo dependency graph
```

### 4. Start working

```bash
mship spawn "add user avatars"     # Create worktrees
mship phase dev                     # Enter development phase
# ... do your work ...
mship log "implemented avatar upload endpoint"
mship test                          # Run tests
mship phase review                  # Enter review phase
mship finish                        # Create coordinated PRs
```

### 5. Clean up

```bash
mship abort --yes                   # Remove worktrees after PRs are merged
```

### For AI agents

Mothership is a control plane that any AI coding agent can call via bash. JSON output is auto-detected when piped, so agents get structured state without extra flags.

**Mothership assumes a disciplined workflow:** plan before you code, test before you review, review before you ship. The phase model (plan → dev → review → run) and soft gates enforce this structure. What mothership does *not* prescribe is how you work within each phase — that's up to your per-repo tools.

We recommend [superpowers](https://github.com/obra/superpowers) for per-repo methodology (TDD, brainstorming, code review). A superpowers skill is included at `skills/working-with-mothership/`. But any agent framework that calls shell commands integrates with mothership — the phases, state, and execution commands are tool-agnostic.

## What Mothership Is Not

- **Not a per-repo methodology** — mothership owns the workflow stages; your per-repo tools (superpowers, custom prompts, CI checks) own the discipline within each stage
- **Not a task runner** — delegates to go-task
- **Not an AI agent** — it's a tool agents call
- **Not limited to one repo layout** — works with single repos, monorepos, and metarepos (multiple repos in a shared workspace)

## Stack

Python 3.14, Typer, Pydantic v2, Rich, InquirerPy, dependency-injector

## License

MIT
