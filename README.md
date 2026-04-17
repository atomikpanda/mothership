# mship

A structured interface between AI coding agents and a running multi-repo system.

> Pre-1.0. API may change. Pin a commit if you need stability.

## Problem

An agent working on a real multi-service codebase needs to do two things well: coordinate with other agents (and its own past sessions) across repos, and observe the running system it's changing. Current tooling gives it neither.

For coordination, agents commit to `main` because they forgot `git checkout`, edit in the wrong worktree, lose track of what changed in repo A while working in repo B, and open cross-repo PRs in the wrong order. These are state failures that git alone doesn't model.

For observation, agents fall back to a grab bag of shell commands and guesswork. Which log file belongs to the service they just changed? Which port is the API on in *their* worktree, not the main checkout? Did Postgres come up before the migration ran? Are they looking at the right database? Without structured answers to these questions, an agent either guesses (and is wrong), pokes around with ad-hoc shell (and is slow), or declares done on unit tests that don't exercise the real system (and ships bugs that only surface in integration).

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

## What mship gives agents

**Isolation with coordination.** Every task gets its own worktree per affected repo on a shared feature branch. A pre-commit hook refuses commits from outside the active task's worktrees. Parallel tasks don't collide. Cross-repo PRs open in dependency order with coordination blocks linking them.

**A running system to observe, not guess at.** `mship run` brings up the task's services in dependency order, waits on per-service healthchecks (`tcp`, `http`, `sleep`, or a custom task), and keeps background services alive so an agent can actually interact with them. Ports and URLs are task-scoped, so two agents working on two tasks don't fight over `localhost:3000`.

**Structured state instead of shell archaeology.** `mship status`, `mship context`, and `mship journal` emit JSON when stdout isn't a TTY. The agent asks for the active repo, the worktree path, the branch, the last test result, the open blockers — it gets structured answers, not text to parse. Combined with the runtime, this means an agent can know which log file belongs to which service, which URL to hit, and which test command to run, without a single `find` or `ps` or `lsof`.

**A phase lifecycle that keeps the agent honest.** `plan → dev → review → run` with soft gates on each transition — warns on moving to dev without a spec, to review with failing tests, to run with uncommitted changes. Blocked tasks (`mship block "waiting on API key"`) are parked explicitly. Phases are the scaffolding that makes the structured state consistent over time.

**A dispatch primitive for session handoff.** `mship dispatch --task <slug> -i "<instruction>"` prints a self-contained subagent prompt — cd directive, branch state, recent journal entries, finish contract — so a fresh agent session can resume without parent-held context.

## How it works

Tasks live in git worktrees managed by mship and tracked in `.mothership/state.yaml`. The runtime layer (`mship run`) reads a topology from `mothership.yaml` — services, dependencies, healthchecks, `env_runner` for secret delegation, `start_mode: background` for long-running services — and brings the stack up in dependency-ordered tiers. The interface layer (`mship status`, `context`, `journal`, `dispatch`) exposes that state to agents as structured output. The coordination layer (phases, audits, the pre-commit hook, `mship finish`) keeps state consistent across sessions and across repos.

Agents plug into this through any MCP server or shell tool they already have. mship doesn't replace `bash`, `playwright-mcp`, or `postgres-mcp`; it tells those tools where to point and what's real.

## Scope

- **Does:** isolate worktrees per task, coordinate cross-repo work, sequence PRs, run dependency-ordered multi-service stacks with healthchecks, expose task-scoped state to agents as structured JSON, emit handoff prompts for subagents.
- **Does not:** run the agent, generate code, manage secrets (delegates to `env_runner`), replace CI.
- **Works for:** a single repo, a monorepo (via `git_root`), or multiple repos in one workspace.

## Reference

- [`docs/cli.md`](docs/cli.md) — full command surface.
- [`docs/configuration.md`](docs/configuration.md) — `mothership.yaml`, healthchecks, service start modes, monorepo rules, task aliasing, drift policy.
- `mship skill install` — installs the agent-side skill bundle (including `working-with-mothership`), which is the canonical guide for agents operating inside an mship workspace.

## License

MIT
