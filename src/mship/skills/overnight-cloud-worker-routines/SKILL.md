---
name: overnight-cloud-worker-routines
description: Use when standing up an overnight/unattended Claude Code cloud routine that becomes a disposable mship worker — clones the workspace, implements one approved spec, and opens the PR(s) through the relay, holding only a low-value per-run token.
---

# Overnight Cloud-Worker Routines

## Overview

A cloud-worker routine is a scheduled Claude Code run that, when it fires,
becomes a **disposable mship worker**: it clones the workspace, implements one
assigned spec, and opens the PR(s) — all routed through the **relay**. The
worker never holds a GitHub token. It carries only a low-value, short-lived
**per-run token**; its git and GitHub-API traffic go through the relay, which
attaches and enforces real credentials at egress (attach-at-relay).

**You** (the agent standing the routine up) do two things the worker cannot:
mint the run token and schedule the routine. The worker does the rest.

> Scheduling a routine and creating it are **agent behaviour** (your own Claude
> Code `/schedule` ability), not mship code. This skill documents the pattern;
> mship does not automate it.

## Prerequisites

- An **approved enrollment** with a **github-app grant** whose ceiling covers
  every repo the run touches (see `mship relay enroll` / `mship relay grant`).
- A live relay egress-server reachable at a base URL (the `<relay-url>` below).
- The approved **spec** the worker will implement.

## The flow

### 1. You: mint a per-run token (scoped to the run)

```bash
mship relay issue-run-token <enrollment-id> \
  --repos "acme/mothership,acme/ground-control" \
  --push-branch "feat/<slug>" \
  --ttl 86400
```

- `--repos` must be within the enrollment's grant ceiling. **It must include
  the workspace repo** (so the worker can clone the workspace to read the
  spec/plan) **and every affected member repo** (so it can clone them, push,
  and open PRs).
- `--push-branch` is the run's branch; the relay only lets the attached
  credential push that branch.
- The token prints **once** — inject it into the routine's environment as the
  `--run-token` value. It is low-value: scoped, short-lived, and useless
  without the relay.

### 2. You: schedule a Claude Code routine

Give the routine an environment that installs mship and the relay URL + the
run token, and a prompt/task naming the spec to implement. Its flow is:

### 3. The worker (inside the fired routine)

```bash
# a. Clone the workspace repo through the relay (git already routes there once
#    bootstrap configures it; the very first workspace clone uses the same relay
#    URL + run token). Then, from the workspace root:

# b. Configure git for the relay and clone the members with NO GitHub token:
mship bootstrap --relay-url "<relay-url>" --run-token "<run-token>"

# c. Fail-fast: verify relay-routed auth can actually push, BEFORE spending AI
#    tokens on code it then can't land. Exits non-zero with a clear message on
#    any failure (invalid/expired token, missing push, unreachable relay):
mship gh preflight --relay-url "<relay-url>" --run-token "<run-token>"

# d. Implement the assigned spec (normal mship phase workflow).

# e. Push + open the PR(s) — routed through the relay by the git config
#    bootstrap wrote (e.g. mship finish).
```

## Guarantees

- **The worker holds only the low-value run token.** No GitHub token, no App
  key, no broker bearer ever lands on the worker. The relay attaches real
  credentials at egress and enforces the run's scope (repos + push branch).
- **Nothing auto-merges — the flow is review-gated end to end.** The worker
  opens PRs; a human reviews and merges. There is no auto-merge step.
- **Fail-fast before spend.** `gh preflight` aborts the run early if relay auth
  can't push, instead of burning AI tokens on code that can't be landed.

## Notes

- The relay flags are opt-in: without `--relay-url`/`--run-token`, `bootstrap`
  and `gh preflight` behave exactly as they do for a local/broker run. Passing
  one without the other is a clear error.
- `git config --global` on the worker is safe **because the worker is
  disposable** (a fresh cloud env). Do not run these flags on a machine whose
  real git config you care about.
