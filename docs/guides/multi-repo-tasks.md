# Multi-repo tasks

**When you need this:** one change spans several repos — a schema, the services
that consume it, the client — and the PRs have to land coherently.

This is the problem mship was built for: the task, not the repo, is the unit of
work.

## One task, many worktrees

```bash
mship spawn "propagate user schema v2" --work-item <wi-id> \
  --repos schemas,svc-users,svc-billing,api,api-client
```

One worktree per repo, all on a shared feature branch
(`feat/propagate-user-schema-v2`). The task tracks which files across which
repos belong to it; `main` stays untouched everywhere.

## Moving between repos

```bash
mship switch svc-users
```

`switch` changes the active repo within the task and prints an orientation
handoff — where you are, the branch state, what changed recently — so you (or
an agent picking up mid-task) don't lose the thread.

## Testing in dependency order

```bash
mship test
# schemas:      pass
# svc-users:    pass
# svc-billing:  FAIL (2 failed)  ← the contract break, caught before any PR
# api:          skipped (upstream failed)
```

Dependency order comes from `mothership.yaml` (see
[Configuration](../configuration.md)): upstream repos test first, so a break in
a consumer is attributed to the change that caused it. Each run also diffs
against the previous iteration — fixes, regressions, and new passes are called
out.

## Task-to-task dependencies

Independent tasks can depend on each other, too:

```bash
mship spawn "client codegen" --work-item <wi-id> --depends-on propagate-user-schema-v2
mship depends list                 # see the edges
mship finish --bypass-deps         # ship a downstream anyway, explicitly
```

## Finishing: PRs that land coherently

```bash
mship finish
```

PRs open in dependency order, each body carrying a coordination block that
links the others — reviewers see the whole change, not five disconnected
diffs. Before finishing, `mship audit` reports per-repo drift (uncommitted
files, unexpected branch state) so nothing ships half-tracked.
