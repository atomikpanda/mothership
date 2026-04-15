# Mothership (`mship`)

**State safety for one AI agent working across repo boundaries.** Makes it structurally impossible for the agent to commit to the wrong branch, modify files in the main worktree, forget what it changed in one repo while working in another, or run tests against a stale version of a dependency.

> ⚠️ **Status: pre-1.0, under heavy development.** Expect breaking changes to config, flags, and state format between commits. Pin a commit if you need stability. Feedback welcome.

## The problem: state hell

Give an AI agent write access across a few repos and you'll see some or all of this within the first week:

- It **commits to main** because it forgot to `git checkout -b` in repo B.
- It **modifies files in the main worktree** instead of the feature branch, because the working directory looked fine and it didn't notice the checkout was stale.
- It **forgets what it changed in repo A** the moment it starts working in repo B — the context window doesn't have repo A's code anymore.
- It **runs tests in repo B against the old version of repo A's code**, because nothing wired the task's repo-A worktree into repo B's dependency resolution.
- It **merges PRs in the wrong order**, breaking `main` on the next CI run.
- It **keeps editing a branch after opening the PR**, updating the PR with accidental changes — or pushing work nobody will ever review.

These aren't agent skill issues. They're state-management issues. One agent, one context window, multiple repos, multiple working trees — the failure modes are structural.

## How mship prevents them

**Worktree isolation is the load-bearing feature.** `mship spawn <description>` creates a git worktree per affected repo at `.worktrees/feat/<slug>/`, each on a matching feature branch. The main checkouts stay pristine. Every subsequent command (`test`, `run`, `logs`, `finish`) operates on those worktrees, not main. The agent literally cannot accidentally modify main because its working tree isn't main.

| Failure | How mship makes it impossible |
|---|---|
| Commits to main | `spawn` creates a feature-branch worktree; every mship command resolves to worktree paths, not repo.path |
| Wrong worktree / stale checkout | `audit` blocks `spawn`/`finish` on drift (wrong branch, dirty, behind remote, foreign worktrees) — gated on every lifecycle boundary |
| Forgets cross-repo state | `mship log` + `mship status` + `mship view *` give the agent structured state it can re-inject into context |
| Tests against stale dep version | `mship test` runs repos in dependency order; `mship doctor` verifies the worktree-to-worktree linking your package manager needs (`symlink_dirs`, npm workspaces, etc.) |
| PRs merged out of order | `mship finish` creates coordinated PRs in dependency order with a cross-repo coordination block in each PR description |
| Agent keeps editing after PR | `mship finish` stamps `finished_at`; `phase dev`/`plan`/`review` refuse transitions on finished tasks; `mship close` tears down once merged |

Works for **a single repo** (the isolation + phase + audit story still applies) *and* multi-repo workspaces (the coordination story layers on top). Start with one; add repos when the system grows.

**Pre-commit guard.** `mship init` installs a pre-commit hook on every git root. While a task is active, the hook refuses commits anywhere except the task's worktrees — making "committed to main instead of the worktree" structurally impossible without explicit bypass (`git commit --no-verify`). Removed cleanly by editing `.git/hooks/pre-commit`; `mship doctor` warns when the hook is missing.

**Cross-repo context switches.** When the agent moves between repos within a task, `mship switch <repo>` records the checkpoint and emits a structured handoff: what changed in dependency repos since the agent was last here, what it logged in this repo, whether the worktree is clean. The agent re-injects the handoff into its context and continues work grounded in current state — no re-reading every file, no stale mental models, no running tests against the wrong version of a dependency.

**Iteration awareness.** Every `mship test` run gets a numbered iteration file with per-repo status, duration, exit code, and stderr tail. The next run shows the diff: new failures, fixes, regressions. Agents iterating on a test failure get a running log of what changed between attempts instead of re-reading stdout.

## mship in 60 seconds

```bash
$ mship init --detect                           # scaffold mothership.yaml from the current directory
$ mship spawn "add labels to tasks"             # one worktree per repo, matched feature branches, main untouched
Spawned task: add-labels-to-tasks
  Branch: feat/add-labels-to-tasks
  Phase: plan
  shared:       /home/me/dev/shared/.worktrees/feat/add-labels-to-tasks       ← agent edits here
  auth-service: /home/me/dev/auth-service/.worktrees/feat/add-labels-to-tasks ← and here
                                                                              (main checkouts untouched)

$ mship phase dev                                # soft gate warns if no spec exists
WARNING: No spec found — consider writing one before developing
Phase: dev

$ mship test                                    # tests run in dependency order, inside the worktrees
shared: pass
auth-service: pass

$ mship audit                                   # git-state check: clean worktrees? right branch? not behind?
workspace: my-platform
shared:       ✓ clean
auth-service: ✓ clean
0 error(s), 0 info across 2 repos

$ mship phase review && mship finish            # coordinated PRs in correct merge order
auth-service: feat/add-labels-to-tasks → main  ✓ https://github.com/you/auth/pull/42
shared:       feat/add-labels-to-tasks → main  ✓ https://github.com/you/shared/pull/41
Task finished. After merge, run `mship close` to clean up.

$ # ...PRs merged externally...
$ mship close                                   # tears down worktrees, clears state, logs completion
```

That's the whole loop. The worktrees the agent was editing in are gone; `main` is unchanged; nothing leaked; `mship status` is empty and ready for the next `spawn`. Everything below is reference.

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

Mothership ships a bundle of skills — the mship skill plus vendored mship-aware superpowers skills (brainstorming, writing-plans, subagent-driven-development, executing-plans, systematic-debugging, TDD, …). Skills install under a single `mothership:` namespace so they don't collide with other plugins.

**Claude Code** (via plugin marketplace):

```
/plugin marketplace add atomikpanda/mothership
/plugin install mothership@mothership-marketplace
```

**Gemini CLI**:

```bash
gemini extensions install https://github.com/atomikpanda/mothership
```

**Codex** — follow [`.codex/INSTALL.md`](./.codex/INSTALL.md) (clone + symlink).

**All of the above, detected automatically** — one command:

```bash
mship skill install                 # detects claude/gemini/codex, installs via each tool's native method
mship skill install --only gemini   # filter to specific agents
mship skill install --yes           # skip the confirmation prompt
```

Claude Code still needs two slash commands inside the REPL (printed by the installer); Gemini and Codex install fully automatically and update cleanly.

**Other agents (universal fallback)** — use the CLI:

```bash
mship skill list                    # see what's in the bundle
mship skill install --all           # install the whole bundle to ~/.agents/skills/mothership/
```

The mship skill teaches the session-start protocol (`mship status` → `mship log`), phase workflow, command reference, and context recovery. The vendored superpowers skills are mship-aware — they require an active task and point subagents at task worktrees, not `main`.

JSON output is auto-emitted when stdout isn't a TTY — agents get structured state without any flag, humans get Rich-formatted output.

## CLI Reference

```bash
# Lifecycle (the iteration loop)
mship init [--detect | --name N --repo PATH:TYPE]   # scaffold mothership.yaml
mship init --install-hooks            # (re)install pre-commit guard on every git root
mship spawn "description" [--repos a,b] [--skip-setup]   # worktrees + branch + plan phase
mship switch <repo>                   # cross-repo context switch: handoff + record active repo
mship phase plan|dev|review|run [-f] # transition with soft gate warnings
mship block "reason" | mship unblock # park/resume the current task
mship test [--all] [--repos|--tag] [--no-diff]   # dep order; shows diff vs previous iteration
mship log [-]                         # read task log; pass message to append
mship log "msg" [--action X] [--open Y] [--repo R] [--test-state pass|fail|mixed]
mship log --show-open                 # list open questions
mship finish [--base B] [--base-map a=B,b=B] [--push-only] [--handoff] [--force-audit]
mship close [--yes] [--force] [--skip-pr-check]   # tear down worktrees after merge

# Inspection
mship status                          # task, phase, branch, drift, last log, finished warning
mship audit [--repos r] [--json]      # git-state drift (gated on spawn/finish)
mship view status|logs|diff|spec [--watch]   # read-only TUIs for tmux/zellij panes
mship view spec --web                 # serve rendered spec on localhost
mship graph                           # repo dependency graph
mship worktrees                       # list active worktrees
mship doctor                          # validate config + tools (gh, env_runner, Taskfiles)

# Maintenance
mship sync [--repos r]                # fast-forward behind-only clean repos
mship prune [--force]                 # remove orphaned worktrees

# Long-running services
mship run [--repos a,b] [--tag t]     # start services per dependency tier
mship logs <service>                  # tail logs for a service
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
