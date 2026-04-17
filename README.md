# mship

Phase-based workflow engine for AI agents working across one or more git repos.

> Pre-1.0. API may change. Pin a commit if you need stability.

## Problem

AI agents doing real engineering work fail in three predictable ways:

- **State drift.** They commit to `main` because they forgot `git checkout`, edit in the wrong worktree, or lose track of what they changed in repo A while working in repo B.
- **Workflow drift.** They jump from "thinking about the problem" to "opening a PR" with no enforcement of a plan/implement/verify lifecycle, so review-phase checks get skipped.
- **Context loss.** When a session ends or a subagent spawns, the next run starts from scratch — no record of what was decided, what's blocking, or what order to merge PRs in.

These are coordination failures, not model-capability failures. Git doesn't model them. `mship` does.

## Quickstart

```bash
uv tool install git+https://github.com/atomikpanda/mothership.git

cd my-project
mship init --name my-project --detect
mship spawn "add hello world"
cd $(mship status | jq -r '.worktrees | to_entries[0].value')

echo 'print("hello")' > hello.py
git add hello.py && git commit -m "feat: hello world"

mship finish --body-file - <<'EOF'
## Summary
First mship task.
## Test plan
- [x] Runs locally.
EOF
```

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/). Optional: [go-task](https://taskfile.dev) for task execution, [gh](https://cli.github.com) for `mship finish`.

## How it works

A **task** is a unit of work with a slug, a feature branch, and one git worktree per affected repo. Each task moves through four phases:

```
plan ──► dev ──► review ──► run
```

Transitions are explicit (`mship phase dev`) and soft-gated: moving to `dev` without a spec, `review` with failing tests, or `run` with uncommitted changes all warn. A pre-commit hook refuses commits from outside the active task's worktrees. Drift audits (`mship audit`) block `spawn` and `finish` on dirty state, wrong branches, or unfetched remotes.

State lives in `.mothership/state.yaml`. Agents read it via `mship status`, `mship journal`, and `mship context` — all of which emit JSON when stdout isn't a TTY, so they compose cleanly with `jq` and with agent tooling.

## What mship does

**Enforces a lifecycle.** `plan → dev → review → run` with soft gates on each transition. Blocked tasks (`mship block "waiting on API key"`) are parked explicitly and refuse phase changes until unblocked.

**Isolates writes.** Every task gets its own worktree on its own feature branch. The pre-commit hook rejects commits from anywhere else while the task is active.

**Coordinates across repos.** `mship spawn "refactor schema" --repos api,client` creates parallel worktrees on a shared feature branch. `mship test` runs them in dependency order. `mship finish` opens PRs in dependency order with cross-repo coordination notes in each body.

**Runs multi-service topologies.** `mship run` starts background services tier by tier, waiting on per-service healthchecks (`tcp`, `http`, `sleep`, or a custom task) before moving to the next tier. Monorepo subdirectories are first-class via `git_root`.

**Surfaces state to agents.** `mship context` emits a one-shot JSON snapshot of the workspace. `mship dispatch --task <slug> -i "<instruction>"` prints a self-contained subagent prompt — cd directive, branch state, recent journal entries, finish contract — so a fresh agent session can resume without parent-held context. Structured `mship journal` entries (`--action`, `--open`, `--test-state`) make session resume actually work.

**Delegates what it shouldn't own.** Task execution is `go-task`. Secret management is whatever `env_runner` you configure (`dotenvx run --`, `op run --`, `doppler run --`, etc.). PR creation is `gh`. mship coordinates; it does not replace.

## Scope

- **Does:** enforce lifecycle, isolate worktrees, sequence cross-repo work, gate on git-state audits, run dependency-ordered services with healthchecks, surface structured state to agents.
- **Does not:** run the agent, generate code, manage secrets, replace CI.
- **Works for:** a single repo, a monorepo (via `git_root`), or multiple repos in one workspace.

## Reference

- [`docs/cli.md`](docs/cli.md) — full command surface.
- [`docs/configuration.md`](docs/configuration.md) — `mothership.yaml`, healthchecks, service start modes, monorepo rules, task aliasing, drift policy.
- `mship skill install` — installs the agent-side skill bundle (including `working-with-mothership`), which is the canonical guide for agents operating inside an mship workspace.

## License

MIT
