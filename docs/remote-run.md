# Remote Run Machine (`--remote[=role]`)

Some verbs are host-bound: iOS capture (`xcrun simctl`) only runs on macOS, an Android emulator needs its own machine, and generally a `run`/`build`/`capture` target may need hardware/toolchain that isn't on your day-to-day box. `mship run/capture/build --remote[=role]` executes the same go-task target on a **different, already-bootstrapped mship workspace**, reached over the relay, and streams its output back to your terminal.

This doc covers the model, how to configure it, and how to read the failure messages it produces.

**Relay credential boundary:** relay-native registrations resolve a short-lived
bearer at each Git/HTTP boundary through the authenticated host directory.
They do not store a standing daemon token, follow redirects, use a direct route,
or fall back to local execution. A real relay-only dirty-source operator smoke
test remains a release gate; examples and automated tests are not evidence for
that physical-host gate.

## The model

A "run host" is just another `mship` workspace, already set up (`mothership.yaml` present, repos cloned) on the machine you want the verb to actually execute on — a Mac with the right simulator, an Android box, a beefier build machine, whatever. That machine runs:

```bash
mship serve --relay
```

exactly like the phone-pairing flow: it dials **out** to the relay (so NAT/"somewhere else entirely" is fine) and is reached through a directory-selected public relay URL. Relay-mode operators never copy the daemon standing token into their registration.

- The remote **materializes the task's branch** — `git fetch` + a worktree at `.worktrees/<task>/<repo>`, mirroring the local worktree layout. Remote execution always operates on a task's branch; there's no ad-hoc remote run (the remote needs a branch to check out).
- The remote runs the repo's go-task target (`run`/`capture`/`build`) with the verb's existing task/capture variables, using the explicit server runtime environment described below rather than inheriting the daemon's entire environment.
- Output streams back live on stderr (not a final blob), leaving structured command results on stdout parseable. `--quiet` suppresses progress; the remote task's exit code becomes your local process's exit code.
- For `capture`, produced artifacts (`screen.png`, `layout.*`) are pulled home automatically.

The client imposes no read-idle timeout on a valid execution response body:
a quiet compiler or running app must not trigger HTTPX's default five-second
read timeout. Response headers, HTTP error bodies, connection establishment,
writes, and pool acquisition retain five-second timeouts. There is no overall
client run deadline; interrupt the command when you want to stop waiting.
This does not disable timeouts imposed by a proxy or relay on the route.

Without `--remote`, legacy unprofiled `mship run`, `capture`, and `build` behavior is unchanged. A task-bound `run` that selects a repository declaring `run_profiles` deliberately enters the profile-aware path even without an explicit `--profile`; see [Profile-aware runs and observations](#profile-aware-runs-and-observations).

## Declaring roles (`mothership.yaml`)

`mothership.yaml` is public (this repo), so it only ever names **logical roles** — never a URL or token:

```yaml
run_hosts: [ios-sim-host, android-emu-host]

repos:
  ios-app:
    capture:
      platforms: [ios]
    run_host: ios-sim-host   # optional: this repo's default role for --remote
```

`run_hosts` is the workspace's full list of roles anyone on the team might map. A repo can optionally declare `run_host: <role>` as its own default, so `mship capture --remote` (bare, no `=role`) auto-resolves without every operator having to type the role name.

## Mapping a role to a connection (`mship run-host`)

Named host registrations are private. User registrations live in
`$XDG_CONFIG_HOME/mothership/run-hosts.yaml` (or `~/.config/mothership` when
XDG is unset, empty, or relative); project registrations live in the private
`.mothership/run-hosts.yaml`. A same-named project entry replaces the complete
user entry. Neither location belongs in `mothership.yaml`.

```bash
# on the remote machine (the run host itself):
mship pair              # prints a groundcontrol://add?... link (+ QR)

# on your operator machine: reusable registration
mship run-host add studio --scope user --role ios-sim-host \
  --pair-link 'groundcontrol://add?...'

# a project-specific complete replacement (the default scope is project):
mship run-host add studio --role ios-sim-host \
  --url https://mac-abc123.relay.example.com --token <serve-token>

mship run-host list      # safe name, role, URL and winning scope; never token
mship run-host remove studio --scope project
```

Existing local role-to-connection files must be migrated explicitly; preview
first, then apply. Migration writes an owner-private exact backup beside the
original file and preserves each old role as an exact named host:

```bash
mship run-host migrate --scope project
mship run-host migrate --scope project --apply
```

After migration an old role remains restricted to its exact converted host.
Only deliberately opt into a pool with `mship run-host allow-role <role> --all`
or list approved names with repeated `--host NAME`.

`MSHIP_RUN_HOST_<ROLE>_URL` / `MSHIP_RUN_HOST_<ROLE>_TOKEN` env vars are a
per-role override for one already eligible host; both variables can also form a
legacy environment-only registration. They never distribute credentials across
a pooled role (role upper-cased, `-` → `_`).


### Relay-native registration

Direct registrations remain deliberately direct: `--url/--token` and the direct
`--pair-link` keep their existing private `{url, token}` mapping and may use the
documented direct environment overrides. A relay registration stores only
`relay`, `host_id`, `workspace_id`, and the directory-selected `instance_id`;
it never stores a public route, refresh, fleet credential, short-lived bearer,
or daemon standing token.

The relay owner first approves/enrols the host and issues the already-existing
account link with `mship relay fleet-token --label <coordinator>
--relay-domain <relay> --store-dir <relay-store>`. Transfer that
`groundcontrol://add-relay?...` link over a secure out-of-band channel. On the
coordinator, enter it only at the echo-hidden prompt:

```bash
mship run-host pair-relay
mship run-host add studio --role ios-sim-host \
  --relay relay.example.com --host-id <approved-host-id> \
  --workspace-id <workspace-id>
```

`pair-relay` consumes but never mints or displays the fleet credential. A
direct `groundcontrol://add?...` link cannot seed relay pairing. If pairing is
missing/revoked, transfer a new account link and pair again; if directory
selection or instance binding fails, re-enrol/approve the host or correct the
stored identity mapping.

To replace a legacy direct record, select the approved identity first and use
the explicit preview/apply flow. It writes a private byte-for-byte backup before
replacement; it never treats the legacy token as a refresh credential or keeps
it as a hidden fallback:

```bash
mship run-host migrate studio --relay relay.example.com \
  --host-id <approved-host-id> --workspace-id <workspace-id>
mship run-host migrate studio --relay relay.example.com \
  --host-id <approved-host-id> --workspace-id <workspace-id> --apply
```

For a relay record, every dirty-source Git push/delete and every legacy or
typed execution independently authenticates `GET
https://enroll.<relay>/hosts`, selects the stored host/instance, exchanges its
refresh only with that selected HTTPS public origin at `/host/token`, and binds
the configured workspace under `/workspaces/{workspace_id}`. Redirects are
refused. A request may make at most one refreshed retry after a definite
pre-start 401/403; source mutation, cleanup, accepted streams, timeouts, and
framing/disconnect failures are never replayed.

The Mac Studio retirement gate remains operator-only: do not remove its
standing-token mapping or temporary Tailscale Serve forwarder until an operator
has run a real relay-only dirty-source build and recorded safe route,
source-receipt, host/workspace tool-route, completion, and coordinator
process-tree evidence showing `gradle_or_java_build_descendant_execs: 0`.
Loopback/unit evidence is not that gate.
## Using it (`--remote[=role]`)

```bash
mship run --remote                 # bare: auto-resolve the role (repo's declared run_host, else the sole configured run_hosts entry)
mship run --remote=ios-sim-host    # explicit role
mship build --remote=android-emu-host
mship capture --repo ios-app --remote=ios-sim-host
```

`run`/`build --remote` require a resolvable task (`--task`, `MSHIP_TASK`, or cwd) — the remote checks out that task's branch, so there's no ad-hoc remote run. `capture --remote` has the same requirement (no ad-hoc remote capture).

Bare `--remote` (no `=role`) auto-resolves in this order: the target repo's declared `run_host`, else the sole entry in `run_hosts` if there's exactly one. Two or more roles with nothing chosen is an ambiguous-role error (see below).

## Profile-aware runs and observations

`run_profiles` is the opt-in, task-bound path for durable target-aware runs. A
bare run selects each configured repository's `default_run_profile`; when no
default is declared, human TTY output presents a profile chooser, while a
non-interactive run requires `--profile`. `--profile` chooses a configured
profile, `--host` restricts it to a configured host registration, and `--target`
restricts it to a discovered configured target alias:

```bash
# Default profile, or the TTY chooser for each selected profiled repository.
mship run --task feature-a --repos app

# Explicit profile launch, constrained to an approved host and target alias.
mship run --task feature-a --repos app --profile android-usb \
  --host lab-android --target pixel-8
```

Each selected profiled repository must resolve against the active task. Before
any application launches, mship discovers and preflights every selected profile;
the dependency graph's ready boundaries remain in force. Each launched
repository gets one separate run ID. A selected repository with no profiles
keeps the ordinary unprofiled flow; requesting a profile, host, or target for
such a repository is an error rather than a fallback.

### Automatic observation selectors

`capture` and `logs` observe recorded runs rather than selecting a new device.
For a task-bound repository with profiles (or existing recorded candidates),
they automatically reuse exactly one matching active, acknowledged `AppRun`.
Several matching runs use the TTY chooser when available or require a more
specific selector in non-interactive use; no matching run tells the operator to
establish one with `mship run`. The optional selectors narrow **recorded** runs:
`--run-id` selects its identity, and `--profile`, `--host`, and `--target`
filter the profile, original host, and attested target alias respectively.

```bash
# Reuse the sole acknowledged run for app, if there is exactly one.
mship capture --task feature-a --repo app
mship logs app --task feature-a

# Observe a particular recorded run; these do not launch or rediscover a target.
mship capture --task feature-a --repo app --run-id <run-id>
mship logs app --task feature-a --run-id <run-id>
```

The recorded private binding determines the observation platform. `capture`
infers it when `--platform` is omitted; a supplied `--platform` must agree with
the recorded platform. `--kind` remains an artifact-kind selector
(`image`, `layout`, or `all`), not a backend operation selector. A supplied
`--remote` must name one of the recorded host's roles, rather than rerouting
the observation. `logs --all` means every configured service/repository; it
never means every device or every recorded run.

The run ID is an observation identity, not a device selector. An observation
uses the exact recorded host registration, owner reference, generation, source
revision, profile/backend revision, and private binding. A changed host,
configuration, target, source identity, missing binding, inactive run, or
unacknowledged owner is refused. Mship does not transfer source, rerun setup,
rediscover a target, launch a replacement, or fall back to legacy
capture/logging for that observation.

### Profile configuration, host bindings, and examples

`run_profiles` names a backend, eligible host roles, and reviewed options;
`run_backends` maps discovery and operation names through the repository's
`tasks:` mapping. The portable
[five-backend configuration example](../examples/run-targets/mothership.yaml)
is the complete opt-in schema. Its
[root Taskfile](../examples/run-targets/Taskfile.yml) composes executable thin
wrappers for [Android CLI](../examples/run-targets/android-cli/),
[Flutter](../examples/run-targets/flutter/), [iOS simulator](../examples/run-targets/ios/),
[browser](../examples/run-targets/browser/), and
[PlatformIO](../examples/run-targets/platformio/). Use these linked examples
instead of copying unreviewed command lines into public configuration.

Keep executable paths, target aliases, SDK/app templates, device identities,
credentials, and state directories in the owner-private
`$XDG_CONFIG_HOME/mothership/run-target-bindings.yaml` (or
`~/.config/mothership/run-target-bindings.yaml`). Discovery is read-only:
normal launch and observation do not install SDK components, create emulators,
pair or provision devices, start a daemon, or adopt an ambient target. The
selected host must already have the required SDK/toolchain and a prepared
application or simulator state. `host_tools` provides explicit mise tool
installation and readiness inspection only; it does not provision platform
components or targets.

The Android and Flutter backends retain their concrete native owners and binary
provenance rules. Generic project-script backends use the existing
`OwnerContext.from_environ()` lifecycle: call `begin()` before the first owner
mutation, acknowledge ready only after admission, and call
`finish(cleanup_known=...)` after cleanup has proved its result. The iOS
example supports configured simulators; physical iOS stays unavailable until a
concrete native USB owner exists. The browser example manages configured
Playwright instances, not an attached native Safari session.

PlatformIO intentionally maps its durable `run` operation to board monitoring.
Its `upload` mapping is a separate typed, finite operation, not a new generic
CLI command or a lifetime-owning session.

### Owner cleanup and provenance

On normal `mship close`, mship resolves the exact recorded host registration
and asks the exact recorded owner/generation to stop. Only after a confirmed
stop does it mark the run stopped, delete its metadata, and remove an
unreferenced private binding. A failed, unknown, unreachable, changed, or
unmatched host/owner leaves its recovery metadata and private binding intact
and blocks close; it is never redirected to a role-derived replacement host.

Source provenance and binary provenance are distinct. A source snapshot
certifies the worktree revision delivered to a host; it does not attest an APK,
app bundle, or running binary. The internal
`InstallFromResult(result_id, artifact_id, sha256)` capability verifies an
immutable result artifact and digest before granting it to an owner, but it has
no public `mship install`, upload, or attach command. Consumers retain platform
binary verification, installation policy, and their provenance record.

`--update-and-hot-reload` remains Flutter-only: it requires exactly `--run-id`,
one task-bound repository, a recorded active run with `reload`, and no
`--profile`, `--host`, or `--target`. It transfers a certified source update to
that exact owner; it cannot select or recreate a session. Dependency, native
build, Taskfile, or host-tool changes require setup or a fresh launch.

### Session verification boundary

Current evidence is Linux-only supervised fake-tool and HTTP-fixture coverage
of the owner/channel, source handoff, capture framing, and cleanup contracts.
It is not acceptance evidence for Android or iOS hardware, an iOS simulator,
emulator provisioning, browser-engine execution, firmware upload, or a
production relay path.

## Pinned host tools

A repo may optionally declare a strict `host_tools.mise` manifest and native
lock in `mothership.yaml`. The declaration is read only from the selected
host's materialized task worktree; global, ancestor, local, fragment, and
environment-selected mise configuration are isolated and rejected if discovered.

```bash
# Read-only, selected-host diagnosis. Requires one task and repo.
mship doctor --remote=android-emu-host --task feature-a --repo app

# The only command that installs declared mise tools. It runs bare `mise install`,
# rechecks the same declaration, and records a safe host-local readiness receipt.
mship bootstrap --host-tools --remote=android-emu-host --task feature-a --repo app
```

Normal remote execution never installs tools: it verifies readiness before
repository setup and runs the authorized task as `mise exec -- task <actual>`
with mise auto-install disabled. These commands never install Android SDK
components, accept licenses, touch devices, or execute a caller-side probe.

## The two-credential model

Two different credentials are in play, and they never mix:

1. **Relay pairing token** (the run-host's own serve bearer token, handed to you via `mship pair`/`--pair-link` or `--url`/`--token`) — this is what YOUR box uses to authenticate to the REMOTE's `mship serve --relay`. It lives only in the gitignored `.mothership/run-hosts.yaml` on your machine, keyed by role.
2. **The remote's own git credentials** — the remote box fetches the task branch using its own git auth (a normal git credential helper, SSH key, or the [`/gh-token` broker](cloud-agent-auth.md) for a credential-less/cloud remote). No GitHub token ever crosses the wire between your box and the remote.

Nothing secret is ever committed to `mothership.yaml` — that file only ever holds role *names*.

## Where capture artifacts land

`mship capture --remote` writes artifacts to the exact same local path a local capture would use:

```
.mothership/captures/<task-slug|_adhoc>/<UTCts>-<platform>/
```

so `discover_artifacts` and anything reading captures locally (including an agent) sees them unchanged, regardless of whether the capture ran locally or on a remote host.

## Known limitations

These boundaries apply to remote execution:

- **`symlink_dirs` / `bind_files` are not replicated on the remote worktree.** `task setup` now runs there (see "Dependencies are derived there, not copied"), so a repo whose deps come from tracked manifests works. A repo that depends on symlinked gitignored material from your source checkout still does not.
- **Remote task stdout is streamed to your terminal verbatim.** There is no ANSI / control-sequence sanitization — the remote host is trusted. Don't point `--remote` at a host you don't control.
- **A `run_host:` set under a `capture:` block in `mothership.yaml` is silently ignored.** `CaptureConfig` has no `run_host` field; only the **repo-level** `run_host` (documented above under "Declaring roles") is honored. Put `run_host:` directly on the repo, not inside its `capture:` block.

## What travels to the run host

`run --remote`, `build --remote`, and **legacy** `capture --remote` prepare
the source you are looking at, **including work you have not committed**.
Selected live-session capture (`--run-id`, or its sole healthy recorded-session
case) does not: it observes the recorded owner without source preflight,
transfer, or setup. The materialized legacy paths inspect each selected repo
before dispatch and take one of three paths:

- **Working tree differs from HEAD** — tracked edits, untracked files, or both →
  mship builds a commit from your working tree and pushes it **straight to the
  run host**, onto a throwaway ref (`refs/mship/run/<task>/<repo>`). The host
  resets a worktree to that ref and runs it. **Nothing is pushed to origin on
  this path**, and nothing on your machine changes: your HEAD, your branch, your
  index and `git status` are exactly as you left them, and the synthesized commit
  belongs to no branch. mship names it as a throwaway run ref in the output for
  that reason — do not build on it.
- **Clean, but origin is missing the branch or is behind it** → mship pushes the
  branch to origin for you, then dispatches. There is nothing extra to send, so
  this is the old, fast path. If origin has a commit you do not, mship **refuses**
  — a push cannot fast-forward from behind, and the run would execute a commit
  you have never seen. It prints the `git pull --ff-only` that fixes it.
- **Mid-merge, mid-rebase, or with unmerged paths** → mship **refuses**, and
  names the command that unblocks you. Files in that state hold conflict markers,
  and a remote failure over a conflict marker tells you nothing about the edit
  you were making.

It also still refuses a worktree that is not on the task's branch, a repo whose
git state it cannot read, and a worktree that is missing — in each case naming
which, because the remedies differ.

Every repo **the run will actually touch** is checked, not just the one you are
standing in: a task has a branch per repo and the run host materializes each
separately. `--repos` / `--tag` narrow the check as well as the run, so work in
progress in a repo you excluded neither blocks the run nor gets sent.

### Why the run host and not origin

Routing uncommitted work through origin would publish it. `git add -A` sweeps in
untracked files, so a debug dump, a data sample or a throwaway script with a
token in it would land on GitHub. Refs under `refs/mship/` are outside the
default fetch refspec but they are not private — `git ls-remote` enumerates them
and anyone with read access can fetch them, which on a public repo means anyone —
and deleting the ref afterwards does not retract the objects, because they stay
reachable by sha. The destination is your own machine, so there is no reason for
a third party to be in the path. **Real history goes to origin; throwaway state
goes host to host.**

The run host accepts these pushes on a purpose-built endpoint: direct records
use `/git/<repo>`, while relay records use
`/workspaces/{workspace_id}/git/<repo>` after an independently resolved
short-lived bearer. Both accept only declared repos and writes under
`refs/mship/run/*`. It is not a mirror, not a remote you add by hand, and not a
path for real history. Each run force-updates its own ref, and `mship close`
deletes the task's scratch refs from the host.

Nothing here changes what `mship finish` requires. The scratch namespace is not
a branch, is not PR-able, and no code path merges or branches from it — so
nothing reaches a PR unreviewed.

### The guarantee, and where it stops

On the clean path mship pushes the **exact sha it resolved HEAD to** during
inspection — `<sha>:refs/heads/<branch>` — rather than letting git resolve `HEAD`
(or the branch) a second time when the push runs moments later. On the dirty path
the same sha becomes the synthesized snapshot's parent. Either way, if something
else commits in the worktree in between — a subagent, a background job — the run
still carries the commit every check actually cleared.

That guarantee ends at origin, and this is a real limit, not a hypothetical one:
**the commit *pushed* is the commit *inspected*; that is not the same claim as
"the commit *executed* is the commit inspected."** Once a push to origin lands,
the branch there is a mutable ref, and anyone with push access can advance it
before the run host fetches it — after mship has finished checking, on a
different machine's clock, outside this process entirely. Closing that gap would
need the run host to materialize an immutable revision instead of resolving a
branch at fetch time. The **dirty path already does exactly that**: it
materializes a specific commit from a ref nothing else writes, with no fetch at
all. The clean path does not.

### Capture provenance

Legacy remote capture uses the same repo-scoped source preflight as remote
run/build. An unsafe checkout or failed source transfer stops that legacy
capture before dispatch; a `git_root` child uses its canonical repository for
transfer.

Selected live-session capture is the exception. A capture of a recorded run
(`--run-id`, or the sole healthy matching recorded session) is an
`/exec/session-capture` observation: it validates the persisted owner and
capability but performs no source transfer, materialization, setup, launch
replacement, or target rediscovery. It observes the source identity already
recorded for that run, not the caller's current working tree.

Neither kind rebuilds or relaunches the app. Legacy source preparation therefore
does not prove which binary is visible on the device; selected-session
observation does not make an installed binary match a newer local tree. Run or
explicitly update the app first when a fresh build/source handoff is required.

Attached remote evidence records source preparation separately from device
provenance. For dirty legacy trees it names the synthesized snapshot SHA and
throwaway ref, not the parent HEAD. It explicitly states that the remote
checkout and running binary provenance were not verified; it does not label a
remote image as a local clean-HEAD capture.

### Dependencies are derived there, not copied

Git carries source, not `node_modules`. So after materializing, the run host runs
**`task setup`** in that worktree, rebuilding dependencies from the manifests the
push just delivered.

That is keyed, or it would defeat the fast loop this exists to enable:

- setup runs the **first time** a worktree is materialized for a task on that
  host — so the first remote run on a fresh host is the slowest it will ever be,
  a one-time cost rather than a regression;
- and again whenever the repo's declared **`setup_inputs`** (its manifests and
  lockfiles — `package.json`, `uv.lock`, `build.gradle`) differ from what that
  host last set up at.

A source-only edit, the common case, pays nothing. A dependency change pays once.
**A repo that declares no `setup_inputs` gets setup on first materialization
only**, because there is nothing to invalidate against — declaring them is what
buys re-run-on-change. A repo that defines no `setup` target at all is skipped
rather than failed. If setup fails, the run stops and you see setup's own output.

### What does not travel

- **Gitignored files.** `.env` and other secrets, build output, virtualenvs,
  `node_modules`. Where they can be rebuilt from tracked manifests that is now
  setup's job; where they cannot — secrets, platform state — they simply are not
  there, and you put them on the run host yourself.
- **`symlink_dirs` / `bind_files`.** Still not replicated on the run host.
- **Your machine.** The source is exact and the dependency environment is derived
  from it, but the run host is not a clone of your box.

## Internal tool-runner API

This is an adapter-facing Python API and authenticated `POST /exec/tool` route,
not a public shell command. It does not change the legacy `mship run`, `build`,
or `capture` CLI routes.

### Requests, preparation and results

`mship.core.remote_tool.ToolRequest` accepts `task`, a configured `repo`, and
an `argv` tuple; argv values are literal arguments, never joined into shell
source. `task_key` is an alternative to `argv`: it requires an empty `argv` and
is resolved on the host through that repository's configured `tasks` mapping.
An unknown key is invalid; callers cannot name an arbitrary host command through
this field.

`input_files` is a mapping of environment-style names to UTF-8 contents, not a
mapping of caller-selected paths. Reserved host identity names
(`MSHIP_TASK`, `MSHIP_REPO`, and `MSHIP_SOURCE_REVISION`) are rejected.
For a profile request, the host validates `MSHIP_TARGET_REQUEST_FILE` against
its configured profile, backend, logical task, options, and certified source
revision, then derives its own `MSHIP_TARGET_BINDINGS_FILE`. An observation
requires that same validated request input plus its
`MSHIP_TARGET_CONTEXT_FILE`; context is additional recorded-owner authority,
never a substitute for a request. A request that supplies profile inputs
without a valid profile request, supplies context outside observation, or fails
this configured-task validation, is invalid.

The operation registry materializes accepted input contents as randomized,
owner-private `0600` regular files under its private operation directory. Child
environment names hold only those private file paths—never the input contents;
the paths, contents, argv, and environment are not published to the task journal
or result events. The registry checks file ownership and identity before use and
removes the inputs during terminal cleanup. These files are request-scoped
private inputs, not a provisioning channel or a persistent device/session
binding.

`ToolRequest` also accepts explicit `env`, relative `cwd` (default `"."`),
`preparation` (`"discover"`, `"launch"` by default, or `"observe"`),
`source_revision`, `run_ref_repos`, paired `owner_ref` / `generation` for
observation, and output/timeout limits. The server derives the worktree and
rejects an absolute cwd, parent traversal, or a resolved cwd outside it. This is
**cwd confinement, not a filesystem sandbox** for trusted project code.

| Policy | Source/setup behavior | Output and lifetime |
|---|---|---|
| `discover` | Prepare and verify task source; never invoke or update the setup cache | Require positive independent caps, at most 1 MiB stdout and 256 KiB stderr, and a finite positive timeout. Return bounded bytes, not incremental inventory fragments. |
| `launch` | Prepare and verify source; run cached setup when needed; verify source again after setup | Stream stdout/stderr without retaining the complete output in memory. Collection caps must be `None`; an optional timeout bounds the backend process, not preceding source transfer/setup. |
| `observe` | Validate the existing owner; no source transfer, materialization, setup, launch replacement or launch-flock acquisition | Empty argv queries status. Nonempty argv runs a separately owned observation process in the existing context. Collection caps must be `None`; timeout is optional. |

`ToolResult` exposes `status`, `exit_code`, `owner_ref`, `generation`,
`source_revision`, and binary `stdout` / `stderr`. `completed` means an ordinary
process exit, including a nonzero exit code: require **both** `completed` and
exit zero before parsing discovery inventory. Limit, timeout, cancellation and
infrastructure failures do not return a partial discovery payload.

Other statuses are `invalid`, `busy`, `unsupported`, `materialization_error`,
`launch_error`, `stdout_limit`, `stderr_limit`, `timeout`, `cancelled`,
`evidence_error`, `unknown`, `auth_error`, and `protocol_error`. `running` is
valid in a `started` event and as the final answer to a status-only observation,
not as a final answer to an executing request. Pre-admission failures may lack
an owner; accepted results must preserve the full owner/generation/source
identity. Missing or changed success identity is a protocol error.

Admission is typed rather than boolean:
`ToolOperationRegistry.admission_status(task)` returns `available`, `busy`,
`unknown`, or `evidence_error`. `busy` means a live, verified operation owns
that task; `unknown` means durable or in-memory ownership cannot be safely
validated (including a restarted process that found a nonterminal record); and
`evidence_error` means the admission evidence itself could not be read or
validated. Only `available` admits a new launch. Never treat `unknown` or
`evidence_error` as an invitation to replace or signal an operation.

`ToolEvent.kind` is `started`, `stdout`, `stderr`, or `result`; output is binary
`data`, and lifecycle events carry `result`. The callback must consume output
incrementally rather than retain an unbounded event list. Setup output can
precede the backend's `started` event.

### Source, backend, and cleanup entry points

`snapshot_remote_source(*, task_obj, target_repos, config, shell)` returns an
immutable `SourceSnapshot` of certified source revisions. It inspects the chosen
repositories once; dirty repositories become private synthesized Git commits,
while clean repositories retain their inspected HEAD. The snapshot stores source
identities rather than caller paths or a host connection.

`prepare_remote_source(*, task_obj, target_repos, config, shell, host, resolver,
output, on_prepared=None, snapshot=None, on_transfer=None)` resolves a fresh
operation credential before each source mutation and transfers that source. It
returns `PreparedSource(run_ref_repos, source_revisions)`.
Supplying a `SourceSnapshot` is authoritative: preparation neither reinspects nor
resynthesizes the local tree, so each eligible host receives the same certified
revision. `on_transfer(git_repo, ref, sha)` runs only after a successful dirty
source delivery.

`RemoteBackendExecutor(*, task_obj, config, shell, output, store,
event_sink=None, transport=None)` is the internal adapter that snapshots a
repository once, prepares every already-selected eligible host, records each
successful dirty transfer, and sends validated typed tool requests. It has no
local fallback and does not invent a source revision after a host-local
materialization failure.

`resolve_launch(*, config, task, repo_name, profile_name, host_name, remote_role,
target_alias, registry, preferences, execute, choose)` validates the configured
repository/profile/backend, prepares eligible hosts through `execute.prepare`,
discovers candidates, ranks them, and returns a `SelectedTarget`. Despite its
name, it does not launch an application. It requires an injected executor and
selection callback; it is not a CLI parser or caller-provisioning API.

Observation uses the persisted run's launch origin as provenance, not as the
requested follow-up operation. A configured `logs` or `capture` operation must
also be granted by that run's stored capabilities. Request/context JSON rejects
duplicate object members and excessive nesting before spawning any task.

`record_run_ref_receipt(state_dir, *, task, host, repo, ref, sha)` records a
successful scratch-ref transfer against the exact registered host name, scope,
and endpoint fingerprint—never its bearer token. Existing task-close cleanup
uses these receipts through `cleanup_run_refs`; callers may also invoke
`cleanup_recorded_run_refs(task, *, config, store, shell, warn)` explicitly.
Recorded refs are deleted only on the matching host with an expected-SHA lease.
Missing/replaced hosts, changed refs, and failed cleanup retain their receipts
and warn; recorded repositories never fall through to role-derived destinations.
Unrecorded legacy repositories retain their existing cleanup behavior.
If receipt persistence fails after a push, preparation attempts leased rollback
and reports unresolved cleanup if rollback fails. This source-ref cleanup does
not implement Task 7's device-session or selected-context teardown.

`mship.core.remote_client.exec_tool(*, request, host, resolver, event_sink=None,
transport=None)` resolves a credential at the operation boundary and sends the
typed request with redirects disabled. Relay authentication may refresh once
only before any streamed event is accepted; there is no token fallback or local
fallback.

This internal surface powers the profile CLI path and recorded-run observation;
it is not a caller-provisioning or arbitrary-command API. It grants no
automatic device/session recovery, SDK/device provisioning, or public
`mship install`, upload, or attach command. Native/device and real relay
acceptance remain separate evidence boundaries.

### Environment, ownership and durable evidence

The server builds the tool environment from a narrow host-runtime allowlist,
not the daemon's complete environment. Entries present on the host are retained
in these categories:

- **Tooling and user runtime:** `PATH`, `HOME`, `TERM`, `TMPDIR`, `TMP`,
  `TEMP`, `USER`, `LOGNAME`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`,
  `XDG_DATA_HOME`, and `XDG_RUNTIME_DIR`.
- **Locale:** `LANG`, `LC_ALL`, and `LC_CTYPE`.
- **Desktop session:** `DISPLAY`, `WAYLAND_DISPLAY`, and
  `DBUS_SESSION_BUS_ADDRESS`.
- **Android and Java:** `ANDROID_HOME`, `ANDROID_SDK_ROOT`,
  `ANDROID_USER_HOME`, `ANDROID_AVD_HOME`, `JAVA_HOME`, and
  `GRADLE_USER_HOME`.
- **Apple toolchains:** `DEVELOPER_DIR` and `SDKROOT`.

`PATH`, `HOME`, and `LANG` default to `os.defpath`, the server home directory,
and `C.UTF-8` when absent. The request's explicit `env` is then applied, and
the server-owned identity values `MSHIP_TASK`, `MSHIP_REPO`, and
`MSHIP_SOURCE_REVISION` are applied last, so a request cannot override its
task, repository, or certified revision. The inherited base deliberately
excludes daemon secrets and controls: for example `MSHIP_SERVE_TOKEN`,
`MSHIP_GH_TOKEN`, `MSHIP_GH_BROKER_URL`, `MSHIP_GH_APP_ID`,
`MSHIP_GH_APP_KEY`, `MSHIP_WORKSPACE`, and run-host mapping variables are not
ambient tool variables. Put task-command configuration in explicit `env`; do
not depend on daemon credentials or control fields reaching a tool process.
A trusted repository `env_runner` may wrap the command using positional `"$@"`
forwarding; argv values are never interpolated into that shell source.

The server-only `ToolContext(task, repo, worktree, source_revision, env_runner)`
feeds one workspace-scoped `ToolOperationRegistry`. Its `run`, `observe`,
`status`, and `admission_status` methods are the common owner below both wire
adapters. Processes use stdin `DEVNULL`, binary pipes and a new POSIX session.
Unsupported process-group ownership fails before preparation/spawn; Linux
requires usable process-status information. Descendants that deliberately
leave the owned process group/session are outside this contract.

`ShellRunner.spawn_argv(args, cwd, env)` accepts a `Path` or a directory-FD
`int` for `cwd`. Registry launch and observation pass a duplicated, pinned
directory FD, not a pathname reconstructed from that FD. The internal isolated
exec bootstrap keeps that FD private while preserving the exact argv,
server-selected environment, binary pipes, new session, and exec-error
reporting. A spawn factory transfers exclusive `Popen` ownership to the registry
when it returns; it must not poll, wait, communicate with, reap, or otherwise
supervise that process. The registry alone collects output and performs
deadline/disconnect cleanup. `/proc/self/fd/...` is not an API or a supported
macOS cwd mechanism.

The launch root FD pins the worktree directory inode. Before a launch or
nonempty observation executes, the registry verifies that the original
worktree location still names that inode, then descends to the requested cwd
descriptor-relative with no symlink following. A replacement or unverified
root yields a typed refusal rather than running in a substituted directory.

Disconnect/deadline cleanup stops and reaps the owned group and coordinates
outstanding observers before releasing context availability. Uncertain cleanup
is `unknown`, not successful cancellation. A durable terminal status may become
visible while the disconnected HTTP response is still unwinding its task flock;
a competing launch can briefly receive `busy`.

Private records and separate raw stdout/stderr live beneath the configured
workspace state directory's `remote-tool-operations/`, namespaced by canonical
workspace and task hashes. Creation, opening, replacement and publication use
descriptor-relative operations with no-follow checks. Generated operation
directories are `0700`; generated records and output files are `0600`; owner,
regular-file, link-count and privacy checks are made at native-readable safe
checkpoints before metadata or output is trusted. Metadata publication is
atomic and synced. The existing task journal receives only safe
identity/status references—not argv, environment or raw output. Storage failure
is explicit; output is not silently discarded as successful execution.

After restart, terminal records are evidence only. Historical nonterminal
owners become `unknown` and their task remains unavailable; no stored identity
authorizes PID-only signaling, replay, or a new observation process. The runner
does not implement automatic recovery or a reconnect scheduler. Reconcile an
uncertain context through independently verified host maintenance, not by
blindly clearing its admission record.

The wire uses the response's `X-Mship-Exec-Nonce` and bounded length-prefixed
JSON events, with strict base64 for bytes. Request bodies are capped at 128 KiB,
event frames at 2 MiB, and output events at 16 KiB. Child bytes cannot become
control records. Legacy exit/artifact framing remains supported.

### Verification boundary

Linux real-process loopback verification covers dirty exact-source transfer,
discovery without setup, literal argv and environment isolation, launch/setup,
live observation and cancellation isolation, disconnect cleanup, durable status,
output limits, quiet deadlines, and legacy run/build/capture artifact extraction.
The native `remote-tool-runner` spec and `remote-tool-runner-507` task journal
record the implementation commit, test-run references, and observed proof.
Fixture source revisions identify disposable test projects, not the runner
implementation. This is not macOS/device testing or public-relay proof; the
actual #506 integration and relay-path release gate above still applies.


## Immutable declared task results

Typed `POST /exec/tool` requests that resolve a configured `task_key` with
`task_outputs` receive a server-created `MSHIP_OUTPUT_DIR`, immutable
declaration file, and producer-manifest path. On child reaping, the host copies
only each exact declared regular file into private storage by descriptor, hashes
it while copying, and persists a result before transient worktree/run-ref
cleanup. This is generic media support: zero-byte `text/plain`, archives, and
images are all valid when declared; there is no Android/package/device rule.

The existing capture contract is unchanged: capture still owns
`MSHIP_CAPTURE_DIR`, recognized capture names, nonce-framed tar transfer, and
evidence attachment. Declared task results never emit or parse capture tar
frames.

Authenticated clients use the existing serving workspace bearer boundary:

* `GET /task-results?task_slug=…`, `work_item_id=…`, or `repo=…` lists safe
  summaries (at least one selector is required);
* `GET /task-results/{result_id}` returns the safe immutable metadata; and
* `GET /task-results/{result_id}/artifacts/{artifact_id}` streams exactly the
  selected private blob only after a pinned-descriptor digest and length
  pre-pass, with `Content-Length`, `ETag`, and `X-Mship-SHA256`.

Opaque IDs are workspace-scoped. Guessed/cross-workspace IDs are 404,
expiration is 410, unavailable artifacts are 409, and integrity failures are
422 without filesystem, token, manifest, stdout, or endpoint disclosure.
Consumers must select both IDs and require `outcome.status == completed`,
`exit_code == 0`, `availability == published`, and a matching retrieved digest
before use. They separately own platform binary verification, installation,
session policy, and their own provenance record.
## Troubleshooting

Start with `mship net status`. It reports every connectivity edge on this machine
— serve, relay, each run-host role, the GitHub auth model in effect, and whether
git is routed through a relay egress — with a status code and the fix for each
unhealthy one. `mship doctor` reports the same checks inline as a
`connectivity/*` group, and `GET /net/topology` on serve returns the same JSON.

```bash
mship net status               # human topology view
mship net status --json        # the same structure, for scripts
mship net status --no-network  # configured state only, no probes
```

The table below is the reference for what each code means. The first six rows are
states `mship net status` detects for you; the rest surface only while a remote
task is running.

| Symptom | Code | Meaning | Fix |
|---|---|---|---|
| `unknown run-host role '<role>'; not declared in this workspace's \`run_hosts:\` list` | `run_host_unknown_role` | You passed `--remote=<role>` (or a repo declared `run_host: <role>`) but that name isn't in `mothership.yaml`'s `run_hosts:` list — likely a typo. | Add the role to `run_hosts:` in `mothership.yaml`, or fix the typo. |
| `ambiguous run-host: multiple roles are configured (...) and none was specified` | `run_hosts_ambiguous_default` | Bare `--remote` with 2+ roles in `run_hosts:` and no repo-declared default. | Pass `--remote=<role>` explicitly, or declare `run_host: <role>` on the repo. |
| `run-host role '<role>' is declared but has no connection mapped on this machine; run \`mship run-host add <role>\`` | `run_host_unmapped` | The role exists in `mothership.yaml`, but *this* machine never mapped it to a `{url, token}`. | `mship run-host add <role> --pair-link '...'` (get the link by running `mship pair` on the remote). |
| `remote host at <url> is unreachable via relay (...)` | `run_host_unreachable` | Couldn't even connect — the remote isn't running `mship serve --relay`, the relay is down, or the pairing is stale. | Confirm the remote is up and `mship serve --relay` is running there; re-pair if the relay subdomain changed. |
| `remote workspace not bootstrapped at <url> (503)` | `run_host_not_bootstrapped` | The remote's `mship serve --relay` is reachable, but that machine has no workspace config wired in (no `mothership.yaml`, or serve was started without one). | Bootstrap that machine as an mship workspace and restart `mship serve --relay` there. |
| `remote host at <url> rejected the bearer token (401)` | `run_host_stale_token` | The mapped token is wrong or was rotated on the remote. | Re-run `mship run-host add <role>` with a fresh pair link/token. |
| `error: unknown repo(s) ...; known repos: ...` (streamed, then a non-zero exit) | — | The remote's own `mothership.yaml` doesn't have a repo of that name — usually a workspace mismatch between your box and the remote. | Confirm both workspaces declare the same repo names, or pass `--repos` naming a repo the remote actually has. |
| A repo's task lines print, then `error: branch-materialize failed for repo '<repo>': ...` (then a non-zero exit) | — | The remote's `git fetch`/`git worktree add` for that repo's task branch failed — commonly the branch not pushed yet, or a dirty/locked worktree on the remote. | Push the task's branch, or clear the stuck worktree on the remote (`git worktree remove`/`prune`), then retry. |
| Remote task's own output ends with a non-zero `__MSHIP_EXIT__ <code>` | — | The task itself failed on the remote — same as a local failure. The streamed output above the exit line is the task's real stdout/stderr. | Read the streamed output like any other failing `run`/`build`/`capture`. |
| `--remote requires a resolvable task: ...` / `--remote requires an active task: ...` | — | You ran `--remote` with no active/resolvable task. Remote execution always needs a branch to check out. | Pass `--task <slug>`, or run from inside an active task's worktree. |
