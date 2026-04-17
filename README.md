# mship

A structured interface between AI coding agents and a running multi-repo system.

> Pre-1.0. API may change. Pin a commit if you need stability.

## Problem

Consider a single feature that touches five repos: the shared schemas, two microservices that depend on those schemas, the api that sits in front of the microservices, and the api-client consumed by a web app. An agent (or team of them) working on this feature has to hold all five in its head simultaneously — make the schema change, propagate it to both microservices, update the api to match, regenerate the client, and verify the whole thing works end-to-end. Then open five PRs that need to land in the right order.

Today, that coordination is almost entirely in the agent's head, and agents are bad at it. They commit to `main` in the wrong repo because they forgot `git checkout`. They edit the api in the main checkout while their feature branch for the api lives in a worktree they never cd'd into. They lose track of what changed in schemas while working in the microservice. They open the api-client PR before the schemas PR and reviewers can't evaluate either in isolation. They declare done on unit tests in one repo while the contract break with another repo goes unnoticed until integration.

And even when the coordination works, the *observation* fails. The agent has five services in five worktrees. Which log file belongs to the microservice it just changed? Which port is the api on in *this* task's worktree, not the main checkout? Did the schemas migration run before the microservice tried to read the new column? Without structured answers, the agent falls back to a grab bag of shell commands and guesswork — and when it guesses wrong, the failure mode is silent: tests pass in each repo, the feature "works" locally, and the bug surfaces in staging after five PRs have already merged.

mship is built for this shape of problem: a feature that spans multiple repos, an agent that needs to coordinate changes across them and observe the running system as a single thing. It also works for simpler cases — a single repo, a monorepo, or two or three loosely-coupled services — but metarepo-scale coordination is the problem it was designed to solve.

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

The quickstart above is deliberately minimal — one repo, one file — to show the task lifecycle in isolation. For the multi-repo case mship was built for, see [`docs/configuration.md`](docs/configuration.md) for `mothership.yaml` examples with dependency graphs, healthchecks, and `env_runner` delegation, and [`docs/cli.md`](docs/cli.md) for the full command surface including `mship spawn --repos`, `mship switch`, and cross-repo `mship finish`.

## What mship gives agents

**Cross-repo coordination as a first-class concept.** A task in mship is a single unit of work that can span many repos. `mship spawn "propagate user schema v2" --repos schemas,svc-users,svc-billing,api,api-client` creates one worktree per repo on a shared feature branch. `mship test` runs them in dependency order. `mship finish` opens five PRs in dependency order with coordination blocks in each body linking the others. Audits catch drift per repo. The agent operates on "the task" — mship tracks which files across which repos belong to it.

**Isolation that makes coordination safe.** Every worktree is on its own feature branch, separate from main. A pre-commit hook refuses commits from outside the active task's worktrees. Parallel tasks on different features get their own worktree sets and don't collide.

**A running system to observe, not guess at.** `mship run` brings up the task's services in dependency order, waits on per-service healthchecks (`tcp`, `http`, `sleep`, or a custom task), and keeps background services alive so the agent can interact with them. Ports and URLs are task-scoped — two tasks running in parallel don't fight over `localhost:3000`. This matters most for the metarepo case, where "the running system" means five services that have to talk to each other.

**Structured state instead of shell archaeology.** `mship status`, `mship context`, and `mship journal` emit JSON when stdout isn't a TTY. The agent asks for the active repo, the worktree paths, the branches, the last test result per repo, the open blockers — it gets structured answers, not text to parse. Combined with the runtime, this means an agent can know which log file belongs to which service, which URL to hit, and which test command to run in each repo, without a single `find` or `ps` or `lsof`.

**A phase lifecycle that keeps the whole feature honest.** `plan → dev → review → run` with soft gates on each transition — warns on moving to dev without a spec, to review with failing tests *in any affected repo*, to run with uncommitted changes *anywhere in the task*. The lifecycle is task-scoped, not repo-scoped, so a feature across five repos has one phase, not five.

**A dispatch primitive for session handoff.** `mship dispatch --task <slug> -i "<instruction>"` prints a self-contained subagent prompt — cd directive, branch state, recent journal entries, finish contract — so a fresh agent session can pick up a multi-repo task without parent-held context.

## How it works

Tasks live in git worktrees managed by mship and tracked in `.mothership/state.yaml`. The runtime layer (`mship run`) reads a topology from `mothership.yaml` — services, dependencies, healthchecks, `env_runner` for secret delegation, `start_mode: background` for long-running services — and brings the stack up in dependency-ordered tiers. The interface layer (`mship status`, `context`, `journal`, `dispatch`) exposes that state to agents as structured output. The coordination layer (phases, audits, the pre-commit hook, `mship finish`) keeps state consistent across sessions and across repos.

Agents plug into this through any MCP server or shell tool they already have. mship doesn't replace `bash`, `playwright-mcp`, or `postgres-mcp`; it tells those tools where to point and what's real.

## Scope

- **Does:** isolate worktrees per task, coordinate cross-repo work, sequence PRs, run dependency-ordered multi-service stacks with healthchecks, expose task-scoped state to agents as structured JSON, emit handoff prompts for subagents.
- **Does not:** run the agent, generate code, manage secrets (delegates to `env_runner`), replace CI.
- **Works for:** multiple repos in one workspace (the primary case), a monorepo (via `git_root`), or a single repo.

## Reference

- [`docs/cli.md`](docs/cli.md) — full command surface.
- [`docs/configuration.md`](docs/configuration.md) — `mothership.yaml`, healthchecks, service start modes, monorepo rules, task aliasing, drift policy.
- `mship skill install` — installs the agent-side skill bundle (including `working-with-mothership`), which is the canonical guide for agents operating inside an mship workspace.

## License

MIT
