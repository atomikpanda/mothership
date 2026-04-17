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
mship journal       # full narrative of what was happening last session
mship switch <repo>   # if you're about to work in a specific repo, call this first
                       # (snapshots dep SHAs + shows what changed since you were last here)
```

If `mship status` errors with "No mothership.yaml found", you're not in a mothership workspace — skip this skill. If there's no active task, ask the user what to work on, then `mship spawn`.

If you see a previous task is still active and `mship journal` shows recent work, **continue that task** rather than starting fresh. Don't spawn a new task that overlaps with an existing one — mothership will reject duplicate slugs.

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
mship init --install-hooks            # (re)install the pre-commit hook on every git root
mship doctor                          # always run after init
```

### Working on a task

```bash
mship spawn "description" [--repos a,b] [--skip-setup]
mship switch <repo>                   # before starting work in a different repo
mship phase plan|dev|review|run [-f]  # `-f` overrides blocked or finished-task guardrail
mship block "reason" | mship unblock
mship test [--all] [--repos|--tag] [--no-diff]
mship journal "msg" [--action X] [--open Y] [--repo R] [--test-state pass|fail|mixed]
mship journal --show-open                 # what am I blocked on across this task?
mship finish [--base B] [--base-map ...] [--push-only] [--handoff] [--force-audit] [--body-file F | --body TEXT] [--force]
mship close [--yes] [--abandon] [--force] [--skip-pr-check]
```

**`spawn` order:** slugify → worktree per repo → symlink `symlink_dirs` → `task setup` (unless `--skip-setup`) → save state → enter `plan`. If a repo's setup fails, the task still spawns; fix and re-run setup manually.

**MANDATORY after `spawn` (or `switch`): `cd` into the worktree BEFORE editing ANY files.** The spawn output prints the worktree path for each repo. Do not start editing, committing, or running anything task-related until your shell's cwd is inside the worktree. If you start editing from the main checkout, every change lands on the wrong branch and the task's feature branch stays empty. Common signs you're in the wrong place: `git status` shows unrelated changes, `git branch` shows `main` instead of `feat/<slug>`, `mship journal` prints the "running from … not the active repo's worktree" warning.

The pre-commit hook enforces this at the git level: if you try `git commit` anywhere except the task's assigned worktree while a task is active, the commit is refused. Use `git commit --no-verify` to bypass for exceptional cases.

**`switch` is required when crossing repos.** It snapshots each dep's HEAD SHA so the next `switch` back can show "what changed in dependencies since you were last here." Without it, you lose the cross-repo orientation anchor.

**After `mship switch <repo>`, `cd` to the worktree shown at the top of the handoff.**
If you don't, your edits in the shell affect the main checkout, not the feature branch.
`mship journal` and `mship test` will warn when run from outside the active worktree.

**`test` writes a numbered iteration file** under `.mothership/test-runs/<task>/`. The next run shows tags per repo (`new failure`, `fix`, `regression`, `still passing`, `still failing`). Auto-appends a structured log entry with `iteration`, `test_state`, `action="ran tests"`. Iterate until clean before transitioning to `review`.

**Always log structured.** `--action` makes session resume actually work. `--open` flags blockers you'll come back to. `--show-open` lists them. The `repo` field is auto-inferred from `mship switch`'s active repo.

**`finish`:** PR base resolves as `--base-map` entry > `--base` > `repo.base_branch` in config > gh default. Every base is verified on origin before any push; empty branches and missing bases fail fast with no partial state.

**`finish` PR body — write a real one.** By default the PR body is just the task description plus a `Closes #N` footer for any issue refs found in the description, journal, and commit subjects. That's a placeholder, not a body. For agent-driven finishes, pass `--body-file <path>` (or `--body '<inline>'`, or `--body -` for stdin) with a real Summary and Test plan. Empty bodies are rejected — that's deliberate. If you forgot at finish time, follow up immediately with `gh pr edit <url> --body-file <path>`. A bare task-description PR is treated as incomplete.

**`finish --force` — push post-finish commits to existing PRs.** After `finish` once, normal `finish` re-runs are idempotent no-ops. If reviewer feedback or on-device testing requires new commits, make them in the worktree, then run `mship finish --force` to push them to the existing PR(s). Updates `finished_at`, adds a `re-finished` journal entry, and does **not** create a new PR or touch the existing PR body. Without `--force`, finish warns when the worktree has commits past `origin/<branch>` so you don't silently lose work — but it won't push. To update a PR body after re-push, use `gh pr edit <url> --body-file <path>` separately (`--force` and `--body-file` are mutually exclusive).

**`close` gates (in order):**
1. **Requires `finish` first.** Refuses if `task.finished_at is None` unless `--abandon` is passed.
2. **Recovery-path check.** For each repo with commits past its base, verifies at least one of: merged into base locally, pushed to origin at same SHA, has a PR URL. Refuses if any repo has unrecoverable commits.
3. **PR state routing** (via `gh pr view --json state`): all merged → `closed: completed`. All closed unmerged → `closed: cancelled on GitHub`. Any open → refuses unless `--force`. No PRs → `closed: cancelled before finish (abandoned)` when `--abandon`, or `closed: no PRs (pushed via --push-only)` after `finish --push-only`.

`--force` bypasses **every** gate and is destructive — it will delete unrecoverable commits. `--abandon` bypasses only the finish-required gate; recovery-path check still runs.

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
mship journal           # full narrative of what was done
mship journal --last 5  # only the recent entries
```

The state file lives in `<git-main-repo>/.mothership/state.yaml` (anchored to the main repo's `.git`, so it works correctly when you `cd` into a worktree).

**Always log progress before:**
- Ending a session
- Starting a long-running operation (e.g., test suite)
- Switching tasks
- Hitting a blocker

Examples of useful log entries:
- `mship journal "implemented JWT validation in auth/middleware.py, all unit tests passing"`
- `mship journal "stuck on CORS issue with the dev server, need to revisit tomorrow"`
- `mship journal "decided to use sqlc for query generation, see ADR-003"`

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
- **Don't forget to `mship journal`** — your future self (or another agent) reads it on session start.
- **Don't merge PRs out of order** — the coordination block in each PR description shows the correct order.
- **Don't ignore healthcheck failures** — if `mship run` reports a service didn't become ready, the dependent services won't work either.
- **Don't run `mship finish` with failing tests** — run `mship test` first.
- **Don't ship a PR with a placeholder body.** If you didn't pass `--body-file`/`--body` to `mship finish`, the PR body is just the task description — not a Summary + Test plan. Follow up with `gh pr edit <url> --body-file <path>` before declaring done. Reviewers (human or agent) need to know what changed and how it was verified.
- **Don't paste test output into `mship journal`** — after every `mship test`, mship auto-logs a structured entry with iteration, test_state, and action. The iteration file under `.mothership/test-runs/` has stderr for failures.
- **Don't keep editing a worktree after `mship finish`** — once `finish` stamps the task as done, phase transitions are blocked (except `run`). If you need to make changes, open a new task with `mship spawn`.
- **Don't manually edit `.mothership/state.yaml`** — use the CLI commands instead.
- **Don't assume `mship` knows what's running outside of it** — if you started services manually, mothership won't track them. Use `mship run` or accept that `mship status` won't reflect them.
- **Don't `--force-audit` without reading the drift** — the gate is there to stop you from starting work on a dirty/wrong-branch repo. If you bypass, know why; the task log records the bypass.
- **Don't `cd` between worktrees without `mship switch`** — you'll miss cross-repo changes and lose the "since your last switch" anchor. Always call `mship switch <repo>` before starting work in a different repo.
- **Don't edit from the main checkout after `mship spawn`** — always `cd` into the worktree first. If `git branch` shows `main` during task work, you are in the wrong place. Stop, move commits onto the feature branch (`git reset --soft`, checkout the task branch, recommit), and continue from the worktree.
- **Don't use `close --abandon` to paper over state mistakes** — `--abandon` is for "I intentionally chose not to ship this work." If the task state is broken (no branch, no worktree, commits on main instead of the feature branch), **stop and fix the root cause**. Move the commits onto the proper branch, re-run `mship spawn` if the worktree never got created, *then* decide to finish or abandon. The "it ended up on main anyway" reasoning hides the mistake from future reviewers — don't.
- **Don't uninstall the pre-commit hook to work around a refusal** — the hook is refusing because you're in the wrong place. `cd` into the task's worktree instead. If the hook genuinely needs to go, remove the MSHIP-BEGIN..MSHIP-END block from `.git/hooks/pre-commit` manually; `mship doctor` will remind you it's missing.

## Recovering when you find yourself on `main` mid-task

If you realize you've been editing or committing from the main checkout instead of the worktree:

1. **Stop editing.** Don't commit more.
2. Identify what needs to move: `git log --oneline <base>..HEAD` from main shows the commits that belong on the task branch.
3. If the commits exist on main only (not yet pushed): `git reset --soft <base>` in main, then `cd` into the task worktree, `git checkout <task-branch>`, and recommit there.
4. If the commits are already pushed to main: they're in origin history. Cherry-pick them onto the task branch in the worktree (`git cherry-pick <sha>..<sha>`), then decide separately whether to revert the main-branch commits (usually yes — they shouldn't have been pushed to main directly).
5. Re-run `mship status` and `mship audit` to verify state is clean.
6. **Never** run `mship close --abandon` as a "reset button." Fix the commits first.

## Integration with Other Tools

Mothership pairs well with:
- **superpowers** — methodology skills (TDD, brainstorming, code review) within each repo
- **Dagger** — containerized execution, polyglot builds; receives `UPSTREAM_*` env vars from mothership
- **gh** — required for `mship finish` PR creation
- **Custom agent frameworks** — anything that can call shell commands and parse JSON works

Mothership outputs JSON automatically when stdout isn't a TTY:

```bash
# If you're inside a task's worktree or have MSHIP_TASK set,
# `mship status` returns the resolved task's detail:
mship status | jq -r .phase

# With 0 or 2+ active tasks and no anchor, `mship status` returns a
# workspace summary instead; use this to list active task slugs:
mship status | jq '.active_tasks[].slug'

mship journal | jq '.entries[].message'
mship graph | jq '.order'
```

This makes it easy to build automation on top of mship without scraping human-readable text.
