# mship

State safety for an AI agent working across your git repos: isolated worktrees per task, coordinated PRs, durable cross-session state.

> Pre-1.0. API may change. Pin a commit if you need stability.

## Problem

AI agents given write access across repos routinely commit to `main` because they forgot `git checkout`, modify files in the wrong worktree, lose track of what they changed in repo A while working in repo B, and merge coordinated PRs in the wrong order. These are state-management failures, not agent-skill failures — git alone doesn't model them.

## Quickstart

```bash
uv tool install git+https://github.com/atomikpanda/mothership.git

cd my-project
mship init --name my-project --repo .:service
mship spawn "add hello world"
cd $(mship status | jq -r '.worktrees."my-project"')

echo 'print("hello")' > hello.py
git add hello.py
git commit -m "feat: hello world"

mship finish --body-file - <<'EOF'
## Summary
First mship task.
## Test plan
- [x] Runs locally.
EOF
```

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/). Optional: [go-task](https://taskfile.dev), [gh](https://cli.github.com) (for `mship finish`).

## How it works

Each task lives in a git worktree on its own feature branch, one per affected repo. mship manages the worktrees, tracks state in `.mothership/state.yaml`, and gates transitions (`spawn`, `finish`, `close`) on git-state audits. The agent reads structured state via `mship status`, `mship journal`, and `mship context`. A pre-commit hook blocks commits from outside the active task's worktrees.

## Common patterns

**Multi-repo task.** `mship spawn "refactor schema" --repos api,client` creates one worktree per repo on the same feature branch. `mship test` runs them in dependency order. `mship finish` opens PRs in dependency order with cross-repo coordination blocks.

**Agent session handoff.** `mship dispatch --task <slug> -i "<instruction>"` emits a self-contained prompt — cd directive, branch state, recent journal entries, finish contract — for a fresh subagent. No parent-held context required.

## Scope

- **Does:** isolate writes via worktrees, sequence cross-repo work, gate transitions on git state, surface structured state to agents.
- **Does not:** run the agent, generate code, manage secrets (delegates to `env_runner`), replace your CI.
- **Works for:** a single repo, a monorepo (via `git_root`), or multiple repos in one workspace.

## Reference

- [`docs/cli.md`](docs/cli.md) — full command surface.
- [`docs/configuration.md`](docs/configuration.md) — `mothership.yaml` options, healthchecks, service start modes, monorepo rules, task aliasing.
- `mship skill install` — installs the agent-side skill bundle (including `working-with-mothership`).

## License

MIT
