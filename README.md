# Mothership (`mship`)

**A control plane for AI coding agents.** Gives agents the workflow structure they lack on their own: phase tracking, isolated worktrees, dependency-ordered execution, drift detection, and coordinated multi-repo PRs.

> ⚠️ **Status: pre-1.0, under heavy development.** Expect breaking changes to config, flags, and state format between commits. Pin a commit if you need stability. Feedback welcome.

## Why you want this

AI agents (Claude Code, Codex, Gemini CLI) can write code, run tests, and commit — but they don't know **what phase they're in**, whether they should be planning or implementing, if tests should pass before review, or how to coordinate isolated branches across repos. They'll happily skip the spec, forget to run tests, create PRs in the wrong merge order, and work on a worktree that's already drifted from origin.

Mothership fixes that without prescribing how the agent works inside each step:

| Without mship | With mship |
|---|---|
| Agent jumps straight to coding, no spec, no plan | `mship spawn "add avatars"` → isolated worktree, `plan → dev → review → run` phases with soft gates |
| "Tests pass in repo A but fail in repo B — why?" | `mship test` runs dependency-ordered, fail-fast |
| "Which PR do I merge first?" | `mship finish` creates coordinated PRs with merge-order hints |
| Agent works on a stale branch without noticing | `mship audit` blocks `spawn`/`finish` on drift (wrong branch, dirty, behind remote) |
| Worktrees pile up, state leaks | `mship prune` + state-anchored to the main repo's `.git` |

Works for a single repo *and* multi-repo workspaces. Start small; scale when your system grows.

## mship in 60 seconds

```bash
$ mship init --detect                           # scaffold mothership.yaml from the current directory
$ mship spawn "add labels to tasks"             # worktrees + branch + plan phase
Spawned task: add-labels-to-tasks
  Branch: feat/add-labels-to-tasks
  Phase: plan
  shared: /home/me/dev/shared/.worktrees/feat/add-labels-to-tasks

$ mship phase dev                                # transition with a soft warning if no spec
WARNING: No spec found — consider writing one before developing
Phase: dev

$ mship test                                    # run tests across repos in dependency order
shared: pass
auth-service: pass

$ mship phase review && mship finish            # coordinated PRs, correct merge order
auth-service: feat/add-labels-to-tasks → main  ✓ https://github.com/you/auth/pull/42
shared:       feat/add-labels-to-tasks → main  ✓ https://github.com/you/shared/pull/41
```

That's the whole loop. Everything else below is reference.

## How it fits

Mothership is not an agent and not a task runner. It's the layer between them:

```
Agent (Claude Code, Codex, Gemini CLI)
    ↓ calls
Mothership (phases, worktrees, state, coordination)
    ↓ delegates to
go-task  (per-repo test/run/lint/setup commands)
```

Each repo owns a `Taskfile.yml` with the commands `task test`, `task run`, etc. Mothership calls them in the right directory, in the right order, with your secret manager wrapping them (`dotenvx`, `op`, `doppler`…).

## Quick start

```bash
# 1. Install
uv tool install git+https://github.com/atomikpanda/mothership.git

# 2. Initialize your workspace
cd ~/my-project
mship init                   # interactive wizard
# or:
mship init --detect          # auto-detect repos in current dir
# or (non-interactive):
mship init --name platform --repo ./shared:library --repo ./api:service:shared

# 3. Verify and start working
mship doctor                 # config + tools check
mship spawn "my first task"
mship phase dev
# ... do work ...
mship log "implemented X"    # breadcrumb for the next session
mship test
mship phase review && mship finish
```

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/). Optional: [go-task](https://taskfile.dev), [gh](https://cli.github.com) (for `mship finish`), [git-delta](https://github.com/dandavison/delta) (for nicer `mship view diff`).

## For AI agents

Mothership ships a skill (compatible with [superpowers](https://github.com/obra/superpowers) and any agent framework that can call bash):

```bash
mship skill install working-with-mothership
```

The skill teaches the session-start protocol (`mship status` → `mship log`), phase workflow, command reference, and context recovery. Installs to `~/.agents/skills/` by default; use `--dest` to override.

JSON output is auto-emitted when stdout isn't a TTY — agents get structured state without any flag, humans get Rich-formatted output.

## CLI Reference

```bash
# Workspace awareness
mship status                          # current phase, task, worktrees, test results; also shows drift, phase duration, last log entry, and a warning if the task is already finished
mship graph                           # show repo dependency graph
mship doctor                          # validate config & tools (gh, env_runner, Taskfiles)

# Phase management
mship phase plan|dev|review|run       # transition with soft gate warnings
mship block "reason" / mship unblock  # park/resume task

# Worktree management
mship spawn "description"             # create worktrees for a new task
mship spawn "desc" --repos a,b        # explicit repo list (multi-repo)
mship worktrees                       # list active worktrees
mship prune [--force]                 # remove orphaned worktrees
mship close [--yes] [--force] [--skip-pr-check]  # discard worktrees, abandon/close task
mship finish [--base <branch>]        # coordinated PRs across repos
mship finish --base-map a=main,b=x    # per-repo PR base overrides
mship finish --push-only              # push branches without opening PRs

# Execution (delegates to go-task per repo)
mship test [--all] [--tag t]          # run tests in dependency order
mship run [--repos a,b]               # start services; foreground or background+healthcheck
mship logs <service>                  # tail logs for a service

# Drift & sync
mship audit [--repos r1,r2] [--json]  # report git-state drift (gated on spawn/finish)
mship sync [--repos r1,r2]            # fast-forward behind-only clean repos
mship spawn|finish --force-audit      # bypass drift gate (logged to task log)

# Live views (for tmux/zellij panes)
mship view status|logs|diff|spec [--watch]   # read-only TUIs with no-yank scroll
mship view spec --web                  # serve rendered spec on localhost

# Context & logs
mship log                             # read task log
mship log "message"                   # append breadcrumb
```

### `mship finish`

#### PR base branch

Each repo's PR can target a non-default base:

```yaml
repos:
  cli:
    path: ../cli
    base_branch: main
  api:
    path: ../api
    base_branch: cli-refactor
```

Overrides (most-specific wins):

- `--base <branch>` — global override for all repos.
- `--base-map cli=main,api=release/x` — per-repo overrides.

`mship finish` verifies every resolved base exists on `origin` before any push. Repos with no configured or overridden base use the remote default branch.

### Drift audit & sync

`mship audit` reports git-state drift across all repos; `mship sync` fast-forwards the clean-behind ones. Both integrate as opt-out gates on `mship spawn` and `mship finish`.

**Per-repo config:**

```yaml
repos:
  schemas:
    path: ../schemas
    expected_branch: marshal-refactor   # optional
    allow_dirty: false                  # default
    allow_extra_worktrees: false        # default
```

**Workspace policy (defaults shown):**

```yaml
audit:
  block_spawn: true
  block_finish: true
```

**Commands:**

- `mship audit [--repos r1,r2] [--json]` — exit 1 on any error-severity drift.
- `mship sync [--repos r1,r2]` — fast-forwards behind-only clean repos; skips the rest with a reason.
- `mship spawn --force-audit` / `mship finish --force-audit` — bypass with a line logged to the task log.

**Issue codes:** `path_missing`, `not_a_git_repo`, `fetch_failed`, `detached_head`, `unexpected_branch`, `dirty_worktree`, `no_upstream`, `behind_remote`, `diverged`, `extra_worktrees` (errors); `ahead_remote` (info-only).

### Live views

`mship view` provides read-only TUIs designed for tmux/zellij panes. All views support `--watch` and `--interval N`.

- `mship view status [--watch]` — current task, phase, worktrees, tests, drift, phase duration, and last log entry
- `mship view logs [task-slug] [--watch]` — tail of the task log
- `mship view diff [--watch]` — per-worktree git diff with untracked files inline
- `mship view spec [name-or-path] [--watch] [--web]` — render newest spec; `--web` serves HTML on localhost

Keys: `q` quit, `j/k` or arrows to scroll, `PgUp/PgDn`, `Home/End`, `r` force refresh.

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

### Monorepo Support (`git_root`)

For monorepos where multiple services share one git repo, use `git_root` to declare subdirectory services:

```yaml
repos:
  backend:
    path: .
    type: service
  web:
    path: web              # relative — interpreted against backend's worktree
    type: service
    git_root: backend
    depends_on: [backend]
```

The subdirectory service shares the parent's worktree. `mship spawn` creates one worktree for `backend` at `.worktrees/feat/<task>/`, and `web`'s effective path becomes `.worktrees/feat/<task>/web`.

Rules:
- `git_root` must reference another repo in the workspace
- The referenced repo cannot itself have `git_root` set (no chaining)
- The subdirectory must exist and contain a `Taskfile.yml`
- Subdirectory services still have their own `depends_on`, `tags`, `tasks`, and `start_mode`

### Service Start Modes (`start_mode`)

For long-running services (dev servers, databases), set `start_mode: background`:

```yaml
repos:
  infra:
    path: ./infra
    type: service
    start_mode: background     # mship run launches and moves on
  backend:
    path: ./backend
    type: service
    start_mode: background
    depends_on: [infra]
  amplify:
    path: ./amplify
    type: service
    # start_mode defaults to foreground
    depends_on: [infra]
```

With `start_mode: background`, `mship run` launches the service in a thread (via `subprocess.Popen`) and continues to the next dependency tier without waiting for exit. Background services keep running until Ctrl-C propagates SIGINT through go-task to their child processes.

`start_mode` only affects `mship run`. Tests and logs always run foreground.

### Healthchecks

For services that need time to become ready (databases, dev servers binding to ports), declare a `healthcheck`. `mship run` waits for the healthcheck to pass before starting dependent services.

```yaml
repos:
  infra:
    path: ./infra
    type: service
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:8001"          # wait for port to accept connections
      timeout: 30s                    # optional, default 30s
      retry_interval: 500ms           # optional, default 500ms

  backend:
    path: ./backend
    type: service
    start_mode: background
    depends_on: [infra]
    healthcheck:
      http: "http://localhost:8000/health"   # wait for 2xx response

  web:
    path: ./web
    type: service
    start_mode: background
    depends_on: [backend]
    healthcheck:
      sleep: 3s                        # unconditional wait

  custom:
    path: ./custom
    type: service
    start_mode: background
    healthcheck:
      task: wait-for-custom            # invokes `task wait-for-custom`, 0 exit = ready
```

**Probe types:**
- `tcp: host:port` — succeeds when a TCP connection is accepted
- `http: url` — succeeds on a 2xx response
- `sleep: duration` — waits unconditionally (for things you can't probe)
- `task: task-name` — runs a Taskfile task; 0 exit = ready

Exactly one probe per healthcheck. If the probe doesn't succeed within `timeout`, the service is treated as failed, background processes are terminated, and `mship run` exits non-zero.

Healthchecks apply to `mship run` only — `mship test` ignores them.

### Task Name Aliasing

If your Taskfile uses different task names than mothership's defaults (`test`, `run`, `lint`, `setup`), add a `tasks:` mapping:

```yaml
repos:
  my-app:
    path: .
    type: service
    tasks:
      run: dev                 # mship run → task dev
      test: test:all           # mship test → task test:all
      lint: lint:all
      setup: infra:start
```

`mship doctor` respects the mapping when checking for standard tasks.

### Taskfile Contract

Each repo needs a `Taskfile.yml` with standard task names. Mothership calls `task <name>` in each repo. Override names per repo in the `tasks` mapping.

Default tasks: `test`, `run`, `lint`, `logs`, `setup`. Missing tasks are skipped gracefully.

## What Mothership is not

- **Not a per-repo methodology** — mothership owns the workflow stages; your per-repo tools (superpowers, custom prompts, CI checks) own the discipline within each stage
- **Not a task runner** — delegates to go-task
- **Not an AI agent** — it's a tool agents call
- **Not limited to one repo layout** — works with single repos, monorepos, and metarepos (multiple repos in a shared workspace)

## Stack

Python 3.14, Typer, Pydantic v2, Rich, InquirerPy, dependency-injector

## License

MIT
