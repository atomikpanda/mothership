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

### Workspace setup

```bash
mship init                              # interactive wizard (humans)
mship init .                            # init in current directory
mship init --name app --repo ./.:service  # non-interactive (single repo)
mship init --name platform --repo ./shared:library --repo ./api:service:shared  # multi-repo
mship init --detect                     # auto-detect repos in current dir

mship doctor                            # validate config, check tools (gh, env_runner, Taskfile parse)
```

Run `mship doctor` after `init` to confirm everything is configured correctly.

### Starting work

```bash
mship spawn "add user avatars"          # creates worktrees + branch, runs setup, enters plan phase
mship spawn "fix auth" --repos shared,auth-service   # only specific repos
mship spawn "quick fix" --skip-setup    # skip the per-repo `task setup` step
```

`mship spawn` does this in order:
1. Slugifies the description into a branch name (`feat/add-user-avatars`)
2. Creates a git worktree per affected repo
3. Symlinks any `symlink_dirs` (e.g., `node_modules`) from the source repo
4. Runs `task setup` in each worktree (unless `--skip-setup`)
5. Saves the task to state, sets it as the current task, enters `plan` phase

If setup fails in any repo, you'll see warnings but the task still spawns — fix the failing repo's setup task and re-run setup manually.

### During work

```bash
mship phase dev                         # transition phase
mship test                              # run tests across affected repos in dependency order (fail-fast)
                                        # shows diff vs previous iteration (new failure / fix / regression tags)
mship test --no-diff                    # skip the diff (plain pass/fail output)
mship test --all                        # run all repos even if one fails
mship test --repos shared,api           # only specific repos
mship test --tag apple                  # repos tagged 'apple' (any number of --tag flags)
mship log "implemented avatar upload, tests passing"  # leave breadcrumbs
mship log "msg" --action "<what I'm doing>"     # structured: records action for session resume
mship log "msg" --open "<blocking question>"    # flag a blocker; re-read with mship log --show-open
mship log --show-open                           # list open questions for the current task
mship status                            # check current state
mship switch <repo>                     # set active repo + emit handoff (deps changed, last log, drift, tests)
mship switch                            # re-render handoff for the currently active repo
```

**Always log progress** after a significant step. The log is what you'll read in your next session if context is wiped.

### Long-running services

```bash
mship run                               # starts services per dependency tier
                                        # foreground services run sequentially
                                        # background services launch in parallel and stay running
mship run --repos backend               # only start specific services
mship run --tag mobile                  # services tagged 'mobile'
```

When `mship run` has any `start_mode: background` services, it blocks the terminal showing a startup summary like:

```
Started 2 background service(s):
  ✓ infra → task dev  (pid 12345)  ready after 1.8s (tcp 127.0.0.1:8001)
  ✓ api   → task dev  (pid 12346)  ready after 0.3s (http :8000/health)

Press Ctrl-C to stop.
```

**Ctrl-C cleanly terminates** all backgrounded services and their child processes via process groups. If a service exits prematurely, mothership reaps any surviving grandchildren (e.g., uvicorn forked in a script).

If a service has a `healthcheck` that fails (e.g., Docker not running so the TCP port never opens), `mship run` exits non-zero with the failure reason and kills any other services that started.

### Tail logs

```bash
mship logs api                          # streams `task logs` for a specific service
```

### Live views (for tmux/zellij panes)

Read-only TUIs designed to be composed into multiplexer layouts. All support `--watch` + `--interval N`, alt-screen, `j/k` scroll with no-yank auto-follow, `q` to quit.

```bash
mship view status [--watch]             # current task, phase, worktrees, tests
mship view logs [task-slug] [--watch]   # tail of the task log
mship view diff [--watch]               # per-worktree git diff; untracked files inline
mship view spec [name] [--watch]        # newest spec in docs/superpowers/specs/
mship view spec --web                   # serves rendered HTML on localhost
```

### Drift audit & sync

`mship audit` and `mship sync` cover git-state drift — complementary to `mship doctor` (which covers tool/config health).

```bash
mship audit                             # reports drift; exit 1 on any error-severity issue
mship audit --repos cli,api             # scope the check
mship audit --json                      # machine-readable output

mship sync                              # fast-forwards clean behind-only repos; skips the rest
mship sync --repos cli,api              # scope the sync
```

**Issue codes** (errors unless noted): `path_missing`, `not_a_git_repo`, `fetch_failed`, `detached_head`, `unexpected_branch`, `dirty_worktree`, `no_upstream`, `behind_remote`, `diverged`, `extra_worktrees`; `ahead_remote` (info).

**Automatic gating.** `mship spawn` and `mship finish` run `audit` scoped to the affected repos. By default, any error blocks the command. Override per-command with `--force-audit` (writes a `BYPASSED AUDIT` line to the task log). Opt out at the workspace level by setting `audit: {block_spawn: false, block_finish: false}` in `mothership.yaml`.

**`mship sync` is strictly safe.** It only ever runs `git fetch --prune` + `git pull --ff-only`. It refuses to switch branches, reset, or touch dirty trees — if a repo isn't cleanly behind the expected branch, it's skipped with a reason.

### Blocked state

```bash
mship block "needs design review"       # marks current task blocked
mship unblock                           # clear the block
mship status                            # shows blocked state in output
```

### Finishing work

```bash
mship phase review                      # transition (warns if tests haven't passed)
mship test                              # confirm everything passes
mship phase run                         # optional: deploy phase
mship finish                            # creates coordinated PRs across repos in dependency order
mship finish --base main                # global override of PR base branch
mship finish --base-map cli=main,api=release/7  # per-repo PR base overrides
mship finish --handoff                  # write a CI handoff manifest instead
mship finish --force-audit              # bypass the drift audit gate (logged to task log)
mship finish --push-only               # push branches without opening PRs
mship close --yes                       # tear down worktrees; routes log entry by PR state
mship close --yes --force               # close even if PRs are still open (logged as forced)
mship close --yes --skip-pr-check       # skip the gh call entirely (offline / no-gh)
```

`mship finish` requires the `gh` CLI installed and authenticated. It creates PRs in dependency order so reviewers see a coordination block in each PR pointing to the others.

PR base branches come from (most-specific wins): `--base-map` entry > `--base` flag > `repo.base_branch` in config > gh default. Every resolved base is verified to exist on origin before any push; missing bases fail fast with no partial state.

After PRs are merged externally, `mship close --yes` cleans up the local worktrees.

### Workspace awareness

```bash
mship graph                             # shows the repo dependency graph and topo order
mship worktrees                         # lists all active worktrees
mship prune                             # dry-run: list orphaned worktrees
mship prune --force                     # clean up orphaned worktrees
```

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
