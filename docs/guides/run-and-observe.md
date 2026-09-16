# Run & observe

**When you need this:** you (or your agent) need the system actually running to
see a change work — not just unit tests passing.

## Bring the stack up

### Profile-free services

The ordinary service workflow is unchanged:

```bash
mship run --repos api,svc-users
mship logs api
mship capture --repo web --platform browser
```

`run` starts the selected services in **dependency order**, waiting on each
repo's healthcheck before starting its dependents — `tcp`, `http`, `sleep`, or
a custom task, declared per repo in `mothership.yaml`
([Configuration](../configuration.md)). Long-running services
(`start_mode: background`) stay alive so you can interact with them. Ports and
URLs are **task-scoped**: two tasks running in parallel don't fight over
`localhost:3000`.

### Profile-aware app sessions (opt-in)

When a task-bound repo declares `run_profiles`, the same `run --repos` command
opens a selected app session rather than changing to a separate command:

```bash
# Run this from the task worktree, or supply --task <task>.
mship run --repos app
# app: run <run-id> is ready

# Observe the acknowledged session that run established.
mship logs app --run-id <run-id>
mship capture --repo app --run-id <run-id> --kind image
```

For a configured repo, bare `run` uses its `default_run_profile`. Without a
default, an interactive terminal offers the configured profiles; a headless
call must name `--profile`. `--host` and `--target` constrain launch selection.
Mship resolves and preflights every selected profile before launching an app,
retains dependency-ready boundaries, and records one run ID per selected repo.

The run ID is an observation identity, not another device selector. `logs` and
`capture` use it to contact the exact recorded owner on its recorded host; they
do not rediscover a target, transfer source, run setup, relaunch the app, or
silently substitute a different session. If an active task/repo has exactly one
acknowledged matching run, `mship logs app` and `mship capture --repo app` reuse
it without `--run-id`. Several matches prompt in a TTY and otherwise require
`--run-id`; a missing, stale, inactive, or uncertain run must be replaced with
a new `mship run`, not recovered by an observation command.

`--profile`, `--host`, and `--target` on `logs` or `capture` filter recorded
runs. When combined with `--run-id`, every filter must agree with that record;
contradictions fail rather than selecting something else. The capture platform
is inferred from the recorded session. Supplying `--platform` only validates
that it agrees; `--kind image|layout|all` chooses the artifact(s), not a
platform, device, or session.

Profiles are opt-in, configured wrappers. They do not adopt an app started
outside mship, install tools or SDK components, create emulators, pair devices,
or provision targets. The portable
[five-backend example](https://github.com/atomikpanda/mothership/blob/main/examples/run-targets/mothership.yaml) shows the
Android-native, Flutter, iOS-simctl, browser, and PlatformIO configuration
shapes; it is not evidence that a host, relay, toolchain, or device is ready.
For the full configuration schema, see [Configuration](../configuration.md);
for selected-host and remote-session behavior, see
[Profile-aware runs and observations](../remote-run.md#profile-aware-runs-and-observations).

The native iOS example launches an already-installed bundle on an already-booted
simulator. It verifies the exact bundle/PID acknowledgement from `simctl launch`
before reporting readiness and refuses a pre-existing app process. Closing its
run terminates that owned app, not the simulator. Launching a Flutter-built bundle
this way is still a native iOS session: it does not establish a Flutter framework
owner or enable hot reload. Transferred source alone does not prove which source
built the installed binary; binary provenance remains unknown without verified
build evidence.

The Flutter example uses its own framework owner. Its simulator inventory is
device-only and advertises operations only for booted targets; discovery never
boots a simulator. Keep the configured Python environment available for the
lifetime of the run: exact-target probes use that same environment. The Flutter
project and reviewed `lib/...` entrypoint must exist at the selected worktree
root with dependencies prepared before launch.

The browser example still has an acceptance blocker: a separate Playwright
connection cannot see the launch connection's page, so its recorded-run logs
and capture do not yet work. Discovery and launch readiness are not proof of
browser observation support. Do not use its advertised observation capabilities
as a release-readiness signal until that owner/control path is corrected.

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

For UI repos without a selected profile session, `mship capture --repo <repo>`
drives that repo's capture target (simulator screenshots, layout dumps) and
files artifacts under `.mothership/captures/<task>/`.

### Promoting a capture to evidence

Most captures are part of the develop–verify–iterate loop: screenshot, look,
adjust, capture again. Those stay ephemeral — `mship capture` writes them under
`.mothership/captures/`, which is gitignored, and nothing else happens.

Passing `--evidence <spec-id>:<criterion-id>` promotes a capture into durable
evidence for that acceptance criterion:

```bash
mship capture --repo app --evidence my-spec:ac3
```

The artifact is copied into `.mothership/evidence/<spec-id>/` under a
content-hashed name, attached to the criterion as `kind=artifact`, and recorded
with the revision it was taken from — marked when that revision is an uncommitted working
tree, and separately marked when the revision itself is not a commit on any
branch (a detached HEAD, or a throwaway ref materialized for a remote capture),
so a reviewer can tell work-in-progress or throwaway evidence from a screenshot
taken at a real, committed revision. Both markers can appear together.

The store is machine-local and gitignored, so it behaves identically whether
your workspace is a metarepo, a monorepo, or a single repo — nothing is ever
committed into your product's history by capturing evidence.

Artifacts are capped at 8 MiB each — a phone fetches these over the relay, and a
screenshot or layout dump larger than that is a capture bug, not evidence. An
over-cap artifact is refused when it is stored (the capture itself still
succeeds) and refused again if one ever reaches the store another way.

**What travels:** the phone fetches evidence from `mship serve` over the relay.
The PR body embeds it when the bytes are fetchable on GitHub, which means
`evidence_storage: published` **and** the bytes actually on a ref GitHub serves.
You do not have to arrange that second half: under `published` storage `mship finish` publishes the
referenced artifacts to an **`mship-evidence` orphan branch in the repo the pull
request targets**, and embeds a raw URL pinned to that branch's commit.

An orphan branch shares no history with your default branch, so the binaries
never enter `main`'s tree and a clone of the product is unaffected. `finish`
pushes that branch and nothing else — never `main`, never your workspace repo —
and when a task spans several repos, each repo that gets a PR publishes to its
own branch, so every PR is self-contained. Published artifacts accumulate on the
branch; nothing prunes them for you.

The publication is built with git plumbing, not a checkout, so your working
tree, index, HEAD and branches are untouched: work you have in flight —
untracked, edited, or already staged — cannot be swept into it. Nothing is
forced: a rejected push, an unreachable origin, or a repo with no GitHub origin
all stop the publish rather than push past it.

Every failure there degrades and none of them block the PR: `finish` warns,
names the artifact instead of embedding it, and carries on opening the PR. It
also warns under `local` or `encrypted` storage, where the bytes are not on
GitHub in readable form at all and nothing is published.

**What does not travel:** secrets, platform state, and anything else git cannot
carry.

## Running on another machine

A legacy `run` or `capture` target that needs hardware you do not have (an iOS
simulator on a Mac, an Android box, or a beefier builder) can use a configured
**run host**:

```bash
# Requires an active task (or pass --task <task>).
mship run --remote=ios-sim-host
```

The host must already be configured and ready; mship does not provision its
tools or devices. A selected profile capture remains an observation of the
recorded owner: it performs no source transfer, setup, launch replacement, or
target rediscovery. Setup, host selection, recovery, and the distinction
between legacy remote execution and selected-session observation are covered in
[Profile-aware runs and observations](../remote-run.md#profile-aware-runs-and-observations).
