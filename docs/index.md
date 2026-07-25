# mship

A structured interface between AI coding agents and a running multi-repo system.

> Pre-1.0. API may change. Pin a commit if you need stability.

mship coordinates feature work that spans multiple repos: phase-based workflow,
per-task worktrees, dependency-ordered execution, healthchecks, and a phone
inbox for operator ↔ agent messaging.

## Where to start

- **[Concepts](concepts.md)** — the mental model: workspaces, work items, tasks, phases.
- **[Configuration](configuration.md)** — `mothership.yaml` reference.
- **[CLI reference](cli.md)** — every `mship` command.

## Remote access

- **[Serve over Tailscale](mship-serve-tailscale.md)** — expose `mship serve` on your tailnet.
- **[Relay hosting](relay-hosting.md)** — self-hosted sish relay with per-device subdomains.
- **[Remote run](remote-run.md)** — trigger runs from anywhere.

## Cloud workers

- **[Unattended cloud runner](unattended-cloud-runner.md)** — the end-to-end runbook: setup, per-run lifecycle, security guarantees. Start here.
- **[Attach-at-relay egress proxy](cloud-worker-auth-spine.md)** — the no-credential-on-worker auth model.
- **[The /gh-token broker](cloud-agent-auth.md)** — the simpler trusted-session auth model + GitHub App setup.
- **[Pull-API runner](adapters/claude-routine-runner.md)** — backlog-draining via a scheduled Claude routine.

## Install

```bash
uv tool install git+https://github.com/atomikpanda/mothership.git
```

See the [repository README](https://github.com/atomikpanda/mothership#readme) for the full quickstart.
