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

**What travels:** the phone fetches evidence from `mship serve` over the relay.
The PR body embeds it when the bytes are fetchable on GitHub — which means
`evidence_storage: committed` **and** the evidence commit pushed. Otherwise the
PR names the artifact instead of showing it, and `mship finish` says so.

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
