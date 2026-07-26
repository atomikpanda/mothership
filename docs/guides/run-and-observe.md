# Run & observe

**When you need this:** you (or your agent) need the system actually running to
see a change work — not just unit tests passing.

## Bring the stack up

```bash
mship run
```

`run` starts the task's services in **dependency order**, waiting on each
repo's healthcheck before starting its dependents — `tcp`, `http`, `sleep`, or
a custom task, declared per repo in `mothership.yaml`
([Configuration](../configuration.md)). Long-running services
(`start_mode: background`) stay alive so you can interact with them.

Ports and URLs are **task-scoped**: two tasks running in parallel don't fight
over `localhost:3000`. Filter to part of the stack with `--repos`:

```bash
mship run --repos api,svc-users
```

## Build artifacts

```bash
mship build
```

Same dependency ordering, running each repo's `build` target — schemas generate
before the services that import them compile.

## See what's real

The observation commands answer questions agents otherwise guess at:

```bash
mship status      # active task, phase, branch, per-repo test results, drift
mship context     # full JSON snapshot of workspace state (built for agents)
mship journal     # the task's log: what was done, when, why
mship graph       # the repo dependency graph
mship worktrees   # every active worktree, grouped by task
```

All of these emit JSON when stdout isn't a TTY (or with `mship --json …`), so
an agent gets structured answers — which log belongs to which service, which
URL to hit, which test command runs where — without `find`/`ps`/`lsof`
archaeology.

## Capturing what's on screen

For UI repos, `mship capture` drives the repo's capture target (simulator
screenshots, layout dumps) and files artifacts under
`.mothership/captures/<task>/`.

### Promoting a capture to evidence

Most captures are part of the develop–verify–iterate loop: screenshot, look,
adjust, capture again. Those stay ephemeral — `mship capture` writes them under
`.mothership/captures/`, which is gitignored, and nothing else happens.

Passing `--evidence <spec-id>:<criterion-id>` promotes a capture into durable
evidence for that acceptance criterion:

```bash
mship capture --evidence my-spec:ac3
```

The artifact is copied into `specs/evidence/<spec-id>/` under a content-hashed
name, attached to the criterion as `kind=artifact`, and recorded with the
revision it was taken from — marked when that revision is an uncommitted tree, so
a reviewer can tell work-in-progress evidence from a screenshot taken at a real
commit.

Artifacts are capped at 8 MiB each — a phone fetches these over the relay, and a
screenshot or layout dump larger than that is a capture bug, not evidence. An
over-cap artifact is refused when it is stored (the capture itself still
succeeds) and refused again if one ever reaches the store another way.

**What travels:** the phone fetches evidence from `mship serve` over the relay.
The PR body embeds it when the bytes are fetchable on GitHub, which means
`evidence_storage: committed` **and** the artifact present in a pushed workspace
commit. You do not have to arrange that second half: under `committed` storage
`mship finish` commits the referenced artifacts and pushes the workspace repo
itself, because otherwise the URL in the PR body would point at a file nobody
ever committed.

That is the one place mship writes to your workspace repo, and it stays narrow:
only files under `specs/evidence/<spec-id>/` that a criterion references, staged
and committed by explicit pathspec, so work you have in flight — untracked,
edited, or already staged — is never swept into it. Nothing is forced: a
gitignored evidence directory, a detached HEAD, a diverged branch, an
unreachable origin, or a branch origin does not already have all stop the
publish rather than push past it.

Every failure there degrades and none of them block the PR: `finish` warns,
names the artifact instead of embedding it, and carries on opening the PR. It
also warns under `local` or `encrypted` storage, where the bytes are not on
GitHub in readable form at all and nothing is committed or pushed.

**What does not travel:** secrets, platform state, and anything else git cannot
carry.

## Running on another machine

A run target that needs hardware you don't have (an iOS simulator on a Mac, an
Android box, a beefier builder) can execute on a **run host**:

```bash
mship run --remote=ios-sim-host
```

Same commands, same env contract, output streamed back live. Setup and
troubleshooting: [Remote run](../remote-run.md).
