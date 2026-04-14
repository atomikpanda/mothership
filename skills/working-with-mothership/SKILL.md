---
name: working-with-mothership
description: Use when working in a workspace with mothership.yaml — provides phase-based workflow, coordinated worktrees, dependency-ordered execution, healthchecks, and context recovery via the mship CLI
---

# Working with Mothership

## Overview

Mothership (`mship`) is a control plane for agentic development. It tracks workflow phases, manages git worktrees, executes tasks across repos in dependency order, and gives agents structured state for context recovery.

**You are the brain. Mothership is the coordinator. go-task is the muscle.**

- You decide *what* to build and *how* to architect it
- Mothership tracks state, sequences execution, and surfaces structure
- go-task (per-repo `Taskfile.yml`) runs the actual commands

Works for single repos, monorepos, and metarepos (multiple separate repos in one workspace).

**Announce at start:** "I'm using the working-with-mothership skill for workspace coordination."

## Session Start Protocol

**Every session, before doing anything else:**

```bash
mship status    # current task, phase, active repo, worktrees, drift, last log
mship log       # full narrative of what was happening last session
mship switch <repo>   # if you're about to work in a specific repo, call this first
                       # (snapshots dep SHAs + shows what changed since you were last here)
```

If `mship status` errors with "No mothership.yaml found", you're not in a mothership workspace — skip this skill. If there's no active task, ask the user what to work on, then `mship spawn`.

If you see a previous task is still active and `mship log` shows recent work, **continue that task** rather than starting fresh. Don't spawn a new task that overlaps with an existing one — mothership will reject duplicate slugs.

## Phase Workflow

Four phases progress linearly. Always transition explicitly with `mship phase <target>`.

| Phase | What happens here | Common per-repo skills |
|---|---|---|
| `plan` | Brainstorm requirements, write spec, write implementation plan | brainstorming, writing-plans (superpowers), or your team's spec process |
| `dev` | Implement, write tests, commit | TDD, subagent-driven-development, or your team's coding workflow |
| `review` | Verify tests pass, code review, lint | code-review, verification-before-completion |
| `run` | Start services, integration test, deploy | depends on environment |

**Soft gates** warn (don't block) when preconditions aren't met:
- `phase dev` → warns if no spec is found
- `phase review` → warns if tests haven't passed
- `phase run` → warns if there are uncommitted changes

**Respect warnings.** If you get "tests not passing" entering review, run `mship test` first.

**Blocked tasks** require explicit handling:
```bash
mship block "waiting on API key from ops"   # parks the task with a reason
mship phase dev                              # ERROR if blocked
mship unblock                                # clear and resume
mship phase dev --force                      # transition AND unblock (with warning)
```

The `--force` flag is for cases where you intentionally want to override the block (e.g., the blocker resolved itself).

## Command Reference

The README has the full one-line cheat sheet. This section adds the agent-specific operational notes — the things you wouldn't guess from `--help`.

### Setup

```bash
mship init [--detect | --name N --repo PATH:TYPE[:DEPS]]
mship doctor                          # always run after init
```

### Working on a task

```bash
mship spawn "description" [--repos a,b] [--skip-setup]
mship switch <repo>                   # before starting work in a different repo
mship phase plan|dev|review|run [-f]  # `-f` overrides blocked or finished-task guardrail
mship block "reason" | mship unblock
mship test [--all] [--repos|--tag] [--no-diff]
mship log "msg" [--action X] [--open Y] [--repo R] [--test-state pass|fail|mixed]
mship log --show-open                 # what am I blocked on across this task?
mship finish [--base B] [--base-map ...] [--push-only] [--handoff] [--force-audit]
mship close [--yes] [--force] [--skip-pr-check]
```

**`spawn` order:** slugify → worktree per repo → symlink `symlink_dirs` → `task setup` (unless `--skip-setup`) → save state → enter `plan`. If a repo's setup fails, the task still spawns; fix and re-run setup manually.

**`switch` is required when crossing repos.** It snapshots each dep's HEAD SHA so the next `switch` back can show "what changed in dependencies since you were last here." Without it, you lose the cross-repo orientation anchor.

**After `mship switch <repo>`, `cd` to the worktree shown at the top of the handoff.**
If you don't, your edits in the shell affect the main checkout, not the feature branch.
`mship log` and `mship test` will warn when run from outside the active worktree.

**`test` writes a numbered iteration file** under `.mothership/test-runs/<task>/`. The next run shows tags per repo (`new failure`, `fix`, `regression`, `still passing`, `still failing`). Auto-appends a structured log entry with `iteration`, `test_state`, `action="ran tests"`. Iterate until clean before transitioning to `review`.

**Always log structured.** `--action` makes session resume actually work. `--open` flags blockers you'll come back to. `--show-open` lists them. The `repo` field is auto-inferred from `mship switch`'s active repo.

**`finish`:** PR base resolves as `--base-map` entry > `--base` > `repo.base_branch` in config > gh default. Every base is verified on origin before any push; empty branches and missing bases fail fast with no partial state.

**`close`:** queries each PR's GitHub state via `gh pr view --json state`. All merged → `closed: completed`. All closed unmerged → `closed: cancelled on GitHub`. Any open → refuses unless `--force`. No PRs (e.g. you used `--push-only` or never finished) → `closed: cancelled before finish` or `closed: no PRs (pushed via --push-only)`.

### Inspection

```bash
mship status                          # task, phase, branch, drift, last log, finished warning
mship audit [--repos r] [--json]
mship view status|logs|diff|spec [--watch]
mship view spec --web                 # serves HTML on localhost
mship graph
mship worktrees
```

**`audit` issue codes** (errors unless noted): `path_missing`, `not_a_git_repo`, `fetch_failed`, `detached_head`, `unexpected_branch`, `dirty_worktree`, `no_upstream`, `behind_remote`, `diverged`, `extra_worktrees`; `ahead_remote` (info-only).

**`audit` is automatically gated on `spawn` and `finish`** — any error blocks unless `--force-audit` (which writes a `BYPASSED AUDIT` entry to the task log). Opt out at the workspace level via `audit: {block_spawn: false, block_finish: false}` in `mothership.yaml`.

**Live views** support `--watch` + `--interval N`, alt-screen, `j/k` scroll with no-yank auto-follow, `q` to quit. Designed for tmux/zellij panes; one process per pane.

### Maintenance

```bash
mship sync [--repos r]                # fast-forward behind-only clean repos
mship prune [--force]                 # remove orphaned worktrees
```

**`sync` is strictly safe.** `git fetch --prune` + `git pull --ff-only` only. It never switches branches, never resets, never touches dirty trees — if a repo isn't cleanly behind the expected branch, it's skipped with a reason.

### Long-running services

```bash
mship run [--repos a,b] [--tag t]
mship logs <service>
```

When `mship run` has any `start_mode: background` services, it blocks the terminal showing a startup summary:

```
Started 2 background service(s):
  ✓ infra → task dev  (pid 12345)  ready after 1.8s (tcp 127.0.0.1:8001)
  ✓ api   → task dev  (pid 12346)  ready after 0.3s (http :8000/health)
Press Ctrl-C to stop.
```

**Ctrl-C cleanly terminates** all backgrounded services and their child processes via process groups. mothership reaps surviving grandchildren (e.g., uvicorn forked in a script) on shutdown.

**Healthcheck failure** (e.g., Docker not running, port never opens) → `mship run` exits non-zero with the reason and kills any other services that already started.

## Context Recovery

When a session ends or context is wiped:

```bash
mship status        # task slug, phase, branch, repos, test results, blocked reason
mship log           # full narrative of what was done
mship log --last 5  # only the recent entries
```

The state file lives in `<git-main-repo>/.mothership/state.yaml` (anchored to the main repo's `.git`, so it works correctly when you `cd` into a worktree).

**Always log progress before:**
- Ending a session
- Starting a long-running operation (e.g., test suite)
- Switching tasks
- Hitting a blocker

Examples of useful log entries:
- `mship log "implemented JWT validation in auth/middleware.py, all unit tests passing"`
- `mship log "stuck on CORS issue with the dev server, need to revisit tomorrow"`
- `mship log "decided to use sqlc for query generation, see ADR-003"`

## Configuration Concepts

### Repo types and dependencies

```yaml
repos:
  shared:
    path: ./shared
    type: library
  auth:
    path: ./auth
    type: service
    depends_on: [shared]              # plain string = compile dependency
  api:
    path: ./api
    type: service
    depends_on:
      - {repo: shared, type: compile}  # compile = build-time link
      - {repo: auth, type: runtime}    # runtime = must be running, not built together
```

### Task name aliasing

If your Taskfile uses different names than mship's defaults (`test`, `run`, `lint`, `setup`):

```yaml
repos:
  api:
    tasks:
      run: dev                # mship run → task dev
      test: test:all
      setup: deps:install
```

### Background services + healthchecks

```yaml
repos:
  infra:
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:8001"      # wait for port to accept connections
      timeout: 30s
  api:
    depends_on: [infra]
    start_mode: background
    healthcheck:
      http: "http://localhost:8000/health"
```

Probe types: `tcp`, `http`, `sleep`, `task` (any one per healthcheck).

### Monorepo subdirectories

```yaml
repos:
  backend:
    path: .
  web:
    path: web                    # subdirectory inside backend's git repo
    git_root: backend            # share backend's worktree
    depends_on: [backend]
```

### Symlink heavy directories

```yaml
repos:
  web:
    symlink_dirs: [node_modules]   # symlink from source so spawn doesn't reinstall
```

### Drift policy (per repo)

```yaml
repos:
  schemas:
    path: ../schemas
    expected_branch: marshal-refactor    # optional; enables unexpected_branch check
    allow_dirty: false                   # default
    allow_extra_worktrees: false         # default
    base_branch: main                    # optional; default PR base for finish
```

### Workspace-level audit policy

```yaml
audit:
  block_spawn: true     # default true — audit errors block mship spawn
  block_finish: true    # default true — audit errors block mship finish
```

Set either to `false` to let the command proceed with a warning instead.

### Filter by tag

```yaml
repos:
  ios-app:
    tags: [apple, mobile]
  android-app:
    tags: [android, mobile]
```

Then `mship test --tag mobile` runs both.

## What NOT to Do

- **Don't skip phases** — follow `plan → dev → review → run`. Use `--force` only when you mean to.
- **Don't create worktrees manually** — always use `mship spawn`. Manual worktrees won't have state, won't link, won't get cleanup.
- **Don't forget to `mship log`** — your future self (or another agent) reads it on session start.
- **Don't merge PRs out of order** — the coordination block in each PR description shows the correct order.
- **Don't ignore healthcheck failures** — if `mship run` reports a service didn't become ready, the dependent services won't work either.
- **Don't run `mship finish` with failing tests** — run `mship test` first.
- **Don't paste test output into `mship log`** — after every `mship test`, mship auto-logs a structured entry with iteration, test_state, and action. The iteration file under `.mothership/test-runs/` has stderr for failures.
- **Don't keep editing a worktree after `mship finish`** — once `finish` stamps the task as done, phase transitions are blocked (except `run`). If you need to make changes, open a new task with `mship spawn`.
- **Don't manually edit `.mothership/state.yaml`** — use the CLI commands instead.
- **Don't assume `mship` knows what's running outside of it** — if you started services manually, mothership won't track them. Use `mship run` or accept that `mship status` won't reflect them.
- **Don't `--force-audit` without reading the drift** — the gate is there to stop you from starting work on a dirty/wrong-branch repo. If you bypass, know why; the task log records the bypass.
- **Don't `cd` between worktrees without `mship switch`** — you'll miss cross-repo changes and lose the "since your last switch" anchor. Always call `mship switch <repo>` before starting work in a different repo.

## Integration with Other Tools

Mothership pairs well with:
- **superpowers** — methodology skills (TDD, brainstorming, code review) within each repo
- **Dagger** — containerized execution, polyglot builds; receives `UPSTREAM_*` env vars from mothership
- **gh** — required for `mship finish` PR creation
- **Custom agent frameworks** — anything that can call shell commands and parse JSON works

Mothership outputs JSON automatically when stdout isn't a TTY:

```bash
mship status | jq .phase
mship log | jq '.entries[].message'
mship graph | jq '.order'
```

This makes it easy to build automation on top of mship without scraping human-readable text.
