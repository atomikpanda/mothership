# mship

A structured interface between AI coding agents and a running multi-repo
system. mship owns the coordination an agent is bad at — isolated worktrees per
task, dependency-ordered tests and PRs across repos, a running stack with
healthchecks — and exposes it all as structured state instead of shell
guesswork. You (or your phone) steer; the agent builds; nothing lands on `main`
by accident.

> Pre-1.0. API may change. Pin a commit if you need stability.

## Start here

**[Getting started](getting-started.md)** — install, create a workspace, and
take one task from spawn to a merged PR. Ten minutes, the whole loop.

```bash
uv tool install git+https://github.com/atomikpanda/mothership.git
```

## Guides, by goal

- **[Ship a feature](guides/ship-a-feature.md)** — the spec-first loop: design
  agreed, plan written, gates enforced.
- **[Fix a bug](guides/fix-a-bug.md)** — the shortest safe path to a merged
  fix (and the `--hotfix` escape hatch).
- **[Multi-repo tasks](guides/multi-repo-tasks.md)** — one change across
  several repos, PRs that land coherently.
- **[Run & observe](guides/run-and-observe.md)** — bring the stack up with
  healthchecks; see what's real instead of guessing.
- **[Phone control](guides/phone-control.md)** — approve specs, answer
  decisions, and merge PRs from Ground Control.
- **[Agent-driven development](guides/agent-driven-development.md)** — skills,
  guardrails, and the orchestrator/subagent pattern.

## Understand it

**[Concepts](concepts.md)** — the object model (WorkItem, Spec, Plan, Task,
worktrees), the lifecycle, and the three gates, with diagrams.

## Reference

- **[CLI reference](cli.md)** — every `mship` command.
- **[Configuration](configuration.md)** — the `mothership.yaml` reference.

## Advanced

- **Remote access** — [serve over Tailscale](mship-serve-tailscale.md),
  [self-hosted relay](relay-hosting.md), [remote run hosts](remote-run.md).
- **Cloud workers** — the [unattended cloud runner](unattended-cloud-runner.md)
  runbook and its auth models.
