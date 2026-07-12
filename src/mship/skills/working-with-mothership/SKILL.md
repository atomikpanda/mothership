---
name: working-with-mothership
description: Use when working in a workspace with mothership.yaml — provides phase-based workflow, coordinated worktrees, dependency-ordered execution, healthchecks, and context recovery via the mship CLI
---

# Working with Mothership

## Overview

Mothership (`mship`) is a control plane for agentic development. It tracks workflow phases, manages git worktrees, executes tasks across repos in dependency order, and gives agents structured state for context recovery.

> **New to the model? Start with the concepts map.** The mship **concepts map** explains how **WorkItem, Spec, Plan, Task, worktrees**, and **agents/subagents** relate (with diagrams) — it's the *what-is*; this skill is the *how-to*. It lives at `docs/concepts.md` in the mship source repo (github.com/atomikpanda/mothership); if the source tree is available locally, read it there, otherwise view it in the repo. Read the map first so the commands below have a model to hang on.

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

## Working on multiple tasks at once

Mothership supports multiple active tasks simultaneously. Typical reasons:

- Blocked on review of task A → start task B while waiting.
- Two unrelated investigations in flight; one can progress while the other is idle.
- Long-running `mship finish` / CI on task A while beginning new work on task B.

Each task gets a worktree per affected repo at `.worktrees/<slug>/<repo>/` (the branch is `feat/<slug>`); they coexist cleanly.

### How mship resolves which task a command targets

In order of precedence:

1. **`--task <slug>` flag** — explicit; wins over everything else. Use for one-off commands from any pwd.
2. **`MSHIP_TASK=<slug>` env var** — shell/tab-level default. Useful when you want every `mship` command in a shell to target one task.
3. **cwd-based inference** — if pwd is inside `.worktrees/<slug>/<repo>/`, mship picks `<slug>`. This is the most ergonomic: `cd` into a worktree and every command defaults to that task.

Zero anchors (no `--task`, no `MSHIP_TASK`, pwd outside any worktree) + two active tasks → `AmbiguousTaskError` with a clear message listing the options.

### Typical parallel workflow

```bash
# Inventory active tasks:
mship worktrees

# Open a new terminal tab → enter a different task's worktree:
cd .worktrees/other-task/<repo>

# Every mship command in this tab now targets `other-task`:
mship status
mship journal "picked up work on other-task"

# Or, from any pwd, run a one-off against a specific task:
mship journal --task first-task "blocked on review; switching to other-task"

# Pin a whole script to a task:
MSHIP_TASK=other-task ./scripts/run-integration.sh
```

### Gotchas for multi-task

- `mship run` starts services. If two tasks each run services, ports will conflict unless task-scoped ports are configured in `mothership.yaml`.
- `mship sync` and `mship audit` operate on the workspace, not a single task — no `--task` needed (or honored).
- Resources outside mship's state (docker containers, DB tables, shared caches) are NOT task-scoped. Share where safe; tear down where not.

## Specs: the spec lifecycle (`mship spec`)

In a mothership workspace the **canonical design artifact is a structured `mship spec`**, not an ad-hoc design doc. A spec is the shared communication substrate: a durable, queryable artifact that agents hand off to each other and that humans review/steer from the mobile app over `mship serve`. The `plan` phase is where a spec is authored and approved.

A spec lives at `<workspace>/specs/<date>-<id>.md` — frontmatter (id, title, status, acceptance criteria, open questions, non-goals, risks, bound task) + a body with `Problem` / `User story` / `Approach` sections. Status flows:
`draft → needs_review → approved → dispatched → implemented → archived` (a review sends a spec back to `draft` carrying a `clarification_reason`; any non-terminal status can be `archived`).

**Specs are workspace-level and branch-stable.** The `specs/` directory lives at the workspace root — not inside a member repo's feature branch — so a spec resolves the same way no matter which task/branch is checked out, and `mship view spec` always finds it. Author the spec (during `plan`) **before** `mship spec dispatch` spawns/binds the task; the task then consumes it. **Never hand-edit a spec file inside a task worktree** — that copy would diverge from the canonical one and be invisible to `mship view spec`. Mutate specs only through the `mship spec` commands below. (This is why the old "which branch do I commit the design doc on?" question doesn't arise: `mship spec`s aren't branch-scoped.)

The lifecycle, in order:

```bash
mship spec new --title "<title>"            # create a stub (status: draft)
mship spec draft <id> [--from-text "..."|--from-file <path>]  # emit a drafting prompt to stdout
#   bare `spec draft <id>` emits a generic prompt; supply intent via --from-text or --from-file
#   → run that prompt through an agent to produce SpecDraft JSON, then:
mship spec apply <id> --from-json <file>     # ingest the draft (→ needs_review)
mship spec validate <id>                     # check body structure
mship spec review <id>                       # print the review payload (criteria, questions, context)
mship spec verdict <id> <criterion-id> approved|flagged
mship spec questions <id>                    # list open questions
mship spec ask <id> "<question>"             # add a question
mship spec answer <id> <question-id> "<answer>"
mship spec approve <id> [--bypass-gate]      # → approved (gate: all criteria approved + questions answered)
mship spec request-changes <id> --reason "<why>"   # → draft (carries the reason)
mship spec dispatch <id>                     # bind the approved spec to a task + emit a handoff
```

Review a spec without the CLI via `mship view spec [--web]`, or over HTTP via `mship serve` (the Ground Control phone path).

**The approval gate.** With `require_approved_spec: true` in `mothership.yaml`, `mship phase dev` is **hard-blocked** until a bound, approved spec exists — escape with `mship phase dev --bypass-spec-gate`. **This is opt-in: the default is OFF**, so by default `phase dev` only warns when no spec is found. Spec-first is the recommended methodology regardless of the gate.

## Work items: the cross-artifact spine (`mship item`)

A **work item** is the durable unit of intent that outlives any single spec, task, or thread. Where a `spec` is the design and a `task` is one worktree of execution, a work item groups everything about one piece of work — its spec, its task(s), the phone thread(s) discussing it, and external links (GitHub/Linear/Notion/Jira/url) — so it can drive a phase-aware cockpit (the Ground Control home). Use it when a piece of work spans more than one of those artifacts, or when you want a stable id to hang external tracker links off.

A work item's **phase is derived** from the state of its linked children — `inbox → shaping → ready → in_flight → review → done` — with an optional manual override. `mship item list` surfaces attention flags per item: **A** needs-approval, **D** needs-decision, **B** blocked, **R** needs-review.

```bash
mship item new "<title>" [--kind feature|bug|chore|question]   # prints the new id (wi-<ts>-<hex>)
mship item list                    # one line per item: `<id>  [phase]  <title>  <flags>`  (--json for agents)
mship item show <id>               # full JSON: linked spec/tasks/threads/links + phase_override
                                   #   (use `mship item list --json` for the live derived phase)
mship item link-spec <id> <spec-id>        # bind the design artifact
mship item link-task <id> <task-slug>      # bind an execution worktree (repeatable)
mship item link-url  <id> <url> [--provider github|linear|notion|jira|url] [--title T]
mship item phase <id> <phase>      # manual override ("nudge") when the derived phase is wrong
mship item migrate                 # backfill items from existing specs/tasks (one-time)
```

Typical flow: `item new` at intake → `link-spec` once a spec exists → `link-task` when you `spawn` → the phase advances as those children move, so the cockpit reshapes without manual bookkeeping. Reach for `item phase` only to correct a wrong derivation, not as the normal way to move work forward.

**Raising the attention flags — escalating to the operator.** When you need the human (a decision, an approval, an action), don't just block silently — escalate on the relevant phone thread: `mship ask <thread> "<question>" --option A --option B` posts a tappable decision card, and `mship reply --needs-you <thread> "<ask>"` posts a Home action card. Both are replies into an operator-opened thread; see the `receiving-messages` skill for the full messaging loop.

## Delegating to subagents: `mship context` and `mship dispatch`

Two mship-native primitives for handing work to subagents. Use them instead of hand-rolling task context:

- **`mship dispatch`** — wraps your instruction (`-i/--instruction`, **required**) in a self-contained Markdown prompt for a subagent. Output includes your instruction plus the task slug, worktree path, phase, recent journal entries, affected repos, and per-repo bases. Pipe stdout directly as the `prompt` field of a Claude Code `Task` tool dispatch (or analogous mechanism in Codex / other agent platforms).

  ```bash
  mship dispatch --task my-task -i "implement the parser changes"   # prints a ready-to-use prompt to stdout
  ```

  **Modes (`--mode`).** By default (`implementer`) the prompt scopes the subagent to the single task, tells it to ask clarifying questions, self-review, and report back — and explicitly **not** to open a PR, because the orchestrator owns integration and runs `mship finish` after review. This is what you want for per-task execution under an orchestrator. Pass `--mode standalone` for the alternative contract where the subagent finishes the work and opens its own PR (use it only for genuinely standalone, one-off dispatches).

- **`mship context`** — emits structured JSON for programmatic consumers. Use when feeding state into a non-Claude-Code LLM, logging for audit, or scripting decisions. `jq`-friendly.

  ```bash
  mship context --task my-task | jq '.affected_repos'
  ```

### Decision tree

| You want to… | Use |
|---|---|
| Dispatch a Claude Code (or Codex) subagent to DO work | `mship dispatch` |
| Feed state into a different LLM / script / audit log | `mship context` |
| Know "which repos are in test phase?" | `mship context \| jq` |
| Generate an implementer prompt for a plan task | `mship dispatch` (see `subagent-driven-development`) |

Both accept `--task <slug>` for multi-task disambiguation. When executing a plan task-by-task, use `mship dispatch` as the basis for implementer prompts rather than hand-rolling the task context — it already knows the worktree path, slug, base branches, and recent journal.

## Phase Workflow

Four phases progress linearly. Always transition explicitly with `mship phase <target>`.

| Phase | What happens here | Common per-repo skills |
|---|---|---|
| `plan` | Brainstorm requirements, author + approve an `mship spec` (see Specs above), write implementation plan | brainstorming, writing-plans, or your team's spec process |
| `dev` | Implement, write tests, commit | TDD, subagent-driven-development, or your team's coding workflow |
| `review` | Verify tests pass, code review, lint | code-review, verification-before-completion |
| `run` | Start services, integration test, deploy | depends on environment |

**Soft gates** warn (don't block) when preconditions aren't met:
- `phase dev` → warns if no spec is found (default); **hard-blocks when `require_approved_spec: true`** in `mothership.yaml` — escape with `--bypass-spec-gate`. See [Specs: the spec lifecycle](#specs-the-spec-lifecycle-mship-spec) above.
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
mship spawn "description" --work-item <id> [--repos a,b] [--skip-setup] [--base <branch>] [--depends-on a,b]
mship switch <repo>                   # before starting work in a different repo
mship phase plan|dev|review|run [-f]  # `-f` overrides blocked or finished-task guardrail
mship block "reason" | mship unblock
mship test [--all] [--repos|--tag] [--no-diff]
mship build [--all] [--repos|--tag]   # runs `task build` across repos in dep order
mship capture [--repo R] [--platform P] [--kind image|layout|all] [--out DIR]
mship journal "msg" [--action X] [--open Y] [--repo R] [--test-state pass|fail|mixed]
mship journal --show-open                 # what am I blocked on across this task?
mship finish [--base B] [--base-map ...] [--push-only] [--handoff] [--force-audit] [--body-file F | --body TEXT] [--force] [--require-tests] [--title T] [--body-map ...]
mship close [--yes] [--abandon] [--force] [--skip-pr-check]
```

### Every task needs a WorkItem

`mship spawn` **requires** `--work-item <id>` — create the WorkItem first, then spawn against it:

```bash
mship item new "<title>" --kind feature|bug|chore|question   # prints wi-<id>
mship spawn "<description>" --work-item <id> [--repos a,b]
```

`spawn` refuses without `--work-item`; `--hotfix` overrides it for emergencies and the override is recorded to `.mothership/bypass-log.jsonl`. Kind controls what's required downstream:

- **feature**-kind WorkItems also need an **approved spec** before `mship phase dev` or `mship finish` will proceed — bind and approve one (`mship spec …`, approved in Ground Control or via the CLI), or skip the check with `mship phase dev --bypass-spec-gate` / `mship finish --hotfix` (both logged).
- **bug/chore/question**-kind WorkItems need only the linked WorkItem — no spec required.

This is enforced everywhere a task's identity matters, not just at `spawn`: the `git commit`/`git push` hooks and the PreToolUse edit-guard hook all check that the active task carries a passing WorkItem gate, and refuse (with an actionable message) if it doesn't. Their escape hatch is `MSHIP_BYPASS_GATE=1` (or `git commit`/`push --no-verify`), which is also bypass-logged. See [Work items: the cross-artifact spine](#work-items-the-cross-artifact-spine-mship-item) above for the full WorkItem model.

**`capture` is the UI analog of `test`.** For UI work (mobile screens, web), run
`mship capture` to grab the running app's rendered state — a screenshot (`image`)
and/or a structured layout dump (`layout`) — into files you can read, then compare
against intent and iterate. It delegates to the repo's `capture` go-task target
(adb/simctl/etc.), so the app must already be running (`mship run`); it does not
boot emulators/simulators. Use `--platform` when a repo targets more than one.
It's task-aware but **not** task-required: with an active task it runs in that
task's worktree and files captures under the task; with no task it runs an
ad-hoc capture against the repo's main checkout (pass `--repo` if the workspace
has more than one). Capture observes a *running app*, not worktree source.

**`spawn` order:** slugify → worktree per repo → symlink `symlink_dirs` → `task setup` (unless `--skip-setup`) → save state → enter `plan`. If a repo's setup fails, the task still spawns; fix and re-run setup manually.

**MANDATORY after `spawn` (or `switch`): `cd` into the worktree BEFORE editing ANY files.** The spawn output prints the worktree path for each repo. Do not start editing, committing, or running anything task-related until your shell's cwd is inside the worktree. If you start editing from the main checkout, every change lands on the wrong branch and the task's feature branch stays empty. Common signs you're in the wrong place: `git status` shows unrelated changes, `git branch` shows `main` instead of `feat/<slug>`, `mship journal` prints the "running from … not the active repo's worktree" warning.

The pre-commit hook enforces this at the git level: if you try `git commit` anywhere except the task's assigned worktree while a task is active, the commit is refused. Use `git commit --no-verify` to bypass for exceptional cases.

**`switch` is required when crossing repos.** It snapshots each dep's HEAD SHA so the next `switch` back can show "what changed in dependencies since you were last here." Without it, you lose the cross-repo orientation anchor.

**After `mship switch <repo>`, `cd` to the worktree shown at the top of the handoff.**
If you don't, your edits in the shell affect the main checkout, not the feature branch.
`mship journal` and `mship test` will warn when run from outside the active worktree.

**`test` writes a numbered iteration file** under `.mothership/test-runs/<task>/`. The next run shows tags per repo (`new failure`, `fix`, `regression`, `still passing`, `still failing`). Auto-appends a structured log entry with `iteration`, `test_state`, `action="ran tests"`. Iterate until clean before transitioning to `review`.

**`build` is the compile/artifact analog of `test`.** It runs each affected repo's `task build` in dependency order; `--all` continues past a failing repo (default stops at the first), and `--repos`/`--tag` scope it. Run it before `finish` when a repo has a build step CI will run, so you catch breakage locally rather than in review.

**Always log structured.** `--action` makes session resume actually work. `--open` flags blockers you'll come back to. `--show-open` lists them. The `repo` field is auto-inferred from `mship switch`'s active repo.

**Structured debugging entries.** `mship debug hypothesis|rule-out|resolved` records structured debugging entries into the task journal. `mship test` auto-attaches to the open hypothesis if one exists. See the `systematic-debugging` skill for the full workflow.

**`finish`:** PR base resolves as `--base-map` entry > `--base` > the task's spawn-time `--base` (see stacked PRs below) > `repo.base_branch` in config > gh default. Every base is verified on origin before any push; empty branches and missing bases fail fast with no partial state. `--require-tests` blocks (not just warns) when no passing test evidence exists for the task. `--title` overrides the PR title; `--body-map` sets per-repo bodies when repos need different PR descriptions. `--force`/`-f` re-pushes new commits to an already-finished task's existing PR (useful when iterating post-finish without opening a new task).

**`finish` PR body — write a real one.** By default the PR body is just the task description plus a `Closes #N` footer for any issue refs found in the description, journal, and commit subjects. That's a placeholder, not a body. For agent-driven finishes, pass `--body-file <path>` (or `--body '<inline>'`, or `--body -` for stdin) with a real Summary and Test plan. Empty bodies are rejected — that's deliberate. If you forgot at finish time, follow up immediately with `gh pr edit <url> --body-file <path>`. A bare task-description PR is treated as incomplete.

### Task dependencies

Express that task B depends on task A. `finish` refuses to ship B until every upstream is merged.

```bash
mship spawn "downstream work" --work-item <id> --depends-on a,b   # declare at spawn
mship depends add <upstream-slug> [--task <slug>]       # retrofit on an existing task
mship depends remove <upstream-slug> [--task <slug>]
mship depends list [--task <slug>] [--graph]            # --graph = full workspace DAG
mship finish --bypass-deps                              # override the readiness gate
mship close --cascade        # also remove downstream from state
mship close --detach-downstream   # clear inbound edges, leave downstream alive
```

### Stacked PRs (`spawn --base`)

To stack a task on top of another open task's branch instead of `main`, pass
`--base <branch>` at spawn:

```bash
mship spawn "follow-up fix" --work-item <id> --base feat/auth-middleware-refactor --repos api
```

The worktree is cut from `<branch>` (verified to exist locally or on origin —
fails fast otherwise, no partial state), and the base is recorded on the task so
`mship finish` targets it as the PR base automatically (no need to repeat `--base`
at finish). Base-relative checks (`close` recovery/ancestry, `context` commits-ahead)
also compare against the stacked base. This differs from `--depends-on`, which
records an ordering/readiness edge but still cuts from the configured base. Combine
them when a task both stacks on and depends on the upstream. (#42)

`mship status` exposes the graph under `.resolved_task.dependencies`:

```bash
mship status | jq .resolved_task.dependencies
# { "upstream": [...], "downstream": [...], "blocked": bool, "blocked_by": [...] }
```

`mship dispatch` includes a `## Dependencies` section in the subagent prompt body. `mship reconcile` reports `dependency_stale` for a downstream that's in sync but whose upstream merged after the downstream was created (i.e., the downstream needs a rebase).

No soft/advisory edges in v1 — for "informed by task-a" relationships, use `mship journal`.

### Iterating after `mship finish` (reviewer feedback, CI fixes, typos)

For small post-finish changes — reviewer comments, CI fixes, doc tweaks — use `mship commit <msg>` instead of spawning a new task:

1. Stage the fix with `git add <files>` in the worktree.
2. Run `mship commit "<commit message>"`. This iterates your task's `affected_repos`, commits staged changes in every worktree that has them, pushes to the existing PR (since the task is finished), and appends a journal entry per repo.

For coordinated multi-repo fixes: stage in each worktree you need, then one `mship commit` handles all of them with the same commit message.

For larger changes — new features, significant refactors — spawn a new task via `mship spawn`. Post-finish commits are for small iterations on the same branch.

`mship phase` remains blocked post-finish (you're in review / integration, not re-planning). `mship journal` and `mship test` continue to work.

**`close` gates (in order):**
1. **Requires `finish` first.** Refuses if `task.finished_at is None` unless `--abandon` is passed.
2. **Recovery-path check.** For each repo with commits past its base, verifies at least one of: merged into base locally, pushed to origin at same SHA, has a PR URL. Refuses if any repo has unrecoverable commits.
3. **PR state routing** (via `gh pr view --json state`): all merged → `closed: completed`. All closed unmerged → `closed: cancelled on GitHub`. Any open → refuses unless `--force`. No PRs → `closed: cancelled before finish (abandoned)` when `--abandon`, or `closed: no PRs (pushed via --push-only)` after `finish --push-only`.

`--force` bypasses **every** gate and is destructive — it will delete unrecoverable commits. `--abandon` bypasses only the finish-required gate; recovery-path check still runs.

### Inspection

```bash
mship status                          # task, phase, branch, drift, last log, finished warning
mship audit [--repos r] [--json]
mship pr                              # aggregate PR state across all active tasks
mship view status|journal|diff|spec [--watch]
mship view spec --web                 # serves HTML on localhost
mship graph
mship worktrees
mship serve [--host H] [--port 47100] # read + review/approve write API over the spec + task model;
                                      # bearer MSHIP_SERVE_TOKEN required for non-loopback — the Ground Control phone surface
```

**`mship pr`** iterates active tasks with `pr_urls` and shows per-PR state (`open`/`merged`/`closed`/`unknown`) and base branch. Use this instead of `gh pr view <url>` per task when you want the full cross-task picture — it's the answer to "what's merged vs. still in review across everything I have open?" TTY → table; non-TTY → JSON `{tasks: [{slug, prs: [...]}]}`.

**`audit` issue codes** (errors unless noted): `path_missing`, `not_a_git_repo`, `fetch_failed`, `detached_head`, `unexpected_branch`, `dirty_worktree`, `no_upstream`, `behind_remote`, `diverged`, `extra_worktrees`; `dirty_untracked` (warn — untracked files only, doesn't block); `ahead_remote` (info-only).

**`audit` is automatically gated on `spawn` and `finish`** — any error blocks unless `--force-audit` (which writes a `BYPASSED AUDIT` entry to the task log). Opt out at the workspace level via `audit: {block_spawn: false, block_finish: false}` in `mothership.yaml`.

**Live views** support `--watch` + `--interval N`, alt-screen, `j/k` scroll with no-yank auto-follow, `q` to quit. Designed for tmux/zellij panes; one process per pane.

### Maintenance

```bash
mship sync [--repos r]                # fast-forward behind-only clean repos
mship prune [--force]                 # remove orphaned worktrees
mship bind refresh [--task T] [--repos R] [--overwrite]
```

**`sync` is strictly safe.** `git fetch --prune` + `git pull --ff-only` only. It never switches branches, never resets, never touches dirty trees — if a repo isn't cleanly behind the expected branch, it's skipped with a reason.

**`bind refresh` re-syncs `bind_files` AND `symlink_dirs`** (#71, #111). Both are declared in `mothership.yaml` and populated into each worktree at spawn time — shared configs / prompts / fixtures (`bind_files`), and heavy directories like `node_modules` (`symlink_dirs`). After spawn, source edits don't propagate automatically, so "works in main checkout but not in worktree" is a drift symptom. `mship bind refresh` re-evaluates both for each repo in `task.affected_repos` (or a `--repos` subset). Per-asset outcomes: `copied`, `updated`, `unchanged`, or `skipped`. Worktree-modified files and symlinks pointing at a different target are preserved without `--overwrite` (the command exits 1 if any are skipped). **Real directories at `symlink_dirs` targets are NEVER overwritten** — even with `--overwrite` — to protect user data.

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
- **Don't keep editing a worktree after `mship finish` without using `mship commit`** — once `finish` stamps the task as done, phase transitions are blocked (except `run`). For small post-finish changes (reviewer feedback, CI fixes, doc tweaks), stage your changes and run `mship commit "<msg>"` — it commits and pushes to the existing PR across all affected repos. For larger changes, open a new task with `mship spawn`.
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
- **mship-skills** — the in-tree methodology skills (TDD, brainstorming, code review, …) bundled with mship and installed via `mship skill install`
- **Dagger** — containerized execution, polyglot builds; receives `UPSTREAM_*` env vars from mothership
- **gh** — required for `mship finish` PR creation
- **Custom agent frameworks** — anything that can call shell commands and parse JSON works

Mothership outputs JSON automatically when stdout isn't a TTY:

```bash
# `mship status` always returns the same envelope shape (#128). When a
# task can be resolved from context (cwd / MSHIP_TASK / --task), its
# full detail is under `.resolved_task`:
mship status | jq -r .resolved_task.phase
mship status | jq -r '.resolved_task.worktrees."<repo>"'

# `.active_tasks[]` is always present — use it to list active slugs:
mship status | jq '.active_tasks[].slug'

# `.resolution_source` tells you how the task was resolved
# ("cwd" | "MSHIP_TASK" | "--task" | "only active task"), or null
# when no task resolved:
mship status | jq -r .resolution_source

mship journal | jq '.entries[].message'
mship graph | jq '.order'
```

This makes it easy to build automation on top of mship without scraping human-readable text.

### Forcing output shape (CI / agents): `--json`, `--quiet`, `--no-color`

TTY auto-detection is convenient interactively but non-deterministic at hand-off
boundaries (a CI runner or an agent that captures over a pty looks like a TTY and
gets human output instead of JSON). Three **global** flags — placed *before* the
subcommand — make it explicit (MOS-103):

```bash
mship --json status      # force JSON regardless of TTY (implies --no-color)
mship --quiet finish      # suppress advisory warnings + progress on stderr (errors unchanged)
mship --no-color status   # strip ANSI color from all output
```

Equivalent env vars (for shell-profile / CI-job defaults): `MSHIP_JSON=1`,
`MSHIP_QUIET=1`, `NO_COLOR=1` (per https://no-color.org/).

**Precedence:** CLI flag > env var > TTY auto-detection. A flag forces its setting
on; leaving it off defers to the env var, then to TTY detection — so plain
`mship status | jq` still yields JSON with no flags. Prefer `MSHIP_JSON=1` in a CI
job's environment so every `mship` call in the job is deterministic.
