# Getting started

By the end of this page you'll have a working mship workspace, one finished
task, and one merged PR — the full loop you'll repeat for everything else.

## Install

```bash
uv tool install git+https://github.com/atomikpanda/mothership.git
```

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/). Optional but
recommended: [go-task](https://taskfile.dev) (task execution) and
[gh](https://cli.github.com) (`mship finish` uses it to open PRs).

## Create a workspace

From the directory that contains your repo(s) — one repo, several, or a
monorepo:

```bash
cd my-project
mship init --name my-project --detect
```

`--detect` scans the current directory for git repos and registers them. This
writes two things:

- **`mothership.yaml`** — the workspace config: your repos, their dependency
  order, test/run targets. Committed, shared with the team. Full reference:
  [Configuration](configuration.md).
- **`.mothership/`** — local state (active tasks, journals). Gitignored.

## Create a work item

```bash
mship item new "hello world" --kind chore
# wi-20260725. . . created
```

Every task belongs to a **work item** — the durable record of *why* the work
exists. It's the first of mship's [three gates](concepts.md#the-three-gates):
`chore` and `bug` items go straight to work; `feature` items ask for a design
first (see [Ship a feature](guides/ship-a-feature.md)).

## Spawn the task

```bash
mship spawn "add hello world" --work-item <the-wi-id>
# task 'add-hello-world' spawned
#   my-project: .worktrees/add-hello-world/my-project  (branch feat/add-hello-world)
```

A **task** is the execution unit: mship creates a git **worktree** per affected
repo, all on a shared feature branch, isolated from `main`. Move into it:

```bash
cd $(mship status | jq -r '.resolved_task.worktrees | to_entries[0].value')
```

## Do the work

Edit files normally inside the worktree, then commit:

```bash
echo 'print("hello")' > hello.py
git add hello.py && git commit -m "feat: hello world"
```

While a task is active, mship's guards refuse commits and (for Claude Code
sessions) edits to the repo's *main checkout* — parallel tasks can't collide,
and nothing lands on `main` by accident.

## Test

```bash
mship test
# my-project: pass (12 passed)  — diff vs previous run: no regressions
```

With several repos, `mship test` runs them in **dependency order** — schemas
before the services that consume them — and reports per-repo results.

## Finish

```bash
mship finish --body-file - <<'EOF'
## Summary
First mship task.
## Test plan
- [x] Runs locally.
EOF
# my-project: PR opened https://github.com/you/my-project/pull/1
```

`finish` pushes the branch and opens the PR (PRs, plural, in dependency order
for multi-repo tasks). It also runs the gates: the work item must exist, and
for feature items an approved spec and plan (details:
[Concepts](concepts.md#the-three-gates)).

## Merge, then close

Merge the PR however you normally do (web UI or `gh pr merge`). Then:

```bash
mship close
# Closed: completed (1 prs merged): add-hello-world
```

`close` verifies the PR state, tears down the worktrees, and clears the task
from `.mothership/state.yaml`. The loop is complete.

## Where next

- **[Ship a feature](guides/ship-a-feature.md)** — the spec-first loop for work
  that deserves a design.
- **[Fix a bug](guides/fix-a-bug.md)** — the shortest safe path to a merged fix.
- **[Multi-repo tasks](guides/multi-repo-tasks.md)** — one change across several
  repos, PRs that land coherently.
- **[Run & observe](guides/run-and-observe.md)** — bring the stack up and see
  what's real.
- **[Phone control](guides/phone-control.md)** — approve specs and merge PRs
  from your phone.
- **[Agent-driven development](guides/agent-driven-development.md)** — let an AI
  agent do the building, safely.
