# Remote Run Machine (`--remote[=role]`)

Some verbs are host-bound: iOS capture (`xcrun simctl`) only runs on macOS, an Android emulator needs its own machine, and generally a `run`/`build`/`capture` target may need hardware/toolchain that isn't on your day-to-day box. `mship run/capture/build --remote[=role]` executes the same go-task target on a **different, already-bootstrapped mship workspace**, reached over the relay, and streams its output back to your terminal.

This doc covers the model, how to configure it, and how to read the failure messages it produces.

## The model

A "run host" is just another `mship` workspace, already set up (`mothership.yaml` present, repos cloned) on the machine you want the verb to actually execute on — a Mac with the right simulator, an Android box, a beefier build machine, whatever. That machine runs:

```bash
mship serve --relay
```

exactly like the phone-pairing flow: it dials **out** to the relay (so NAT/"somewhere else entirely" is fine) and is reached at a stable per-device relay URL, bearer-auth'd.

Your local box (the operator) then treats that URL+token as a **run-host role** and, when you pass `--remote`, POSTs to the remote's `/exec/{verb}` endpoint instead of running the task locally:

- The remote **materializes the task's branch** — `git fetch` + a worktree at `.worktrees/<task>/<repo>`, mirroring the local worktree layout. Remote execution always operates on a task's branch; there's no ad-hoc remote run (the remote needs a branch to check out).
- The remote runs the repo's go-task target (`run`/`capture`/`build`) with the **same env-var contract** as a local run.
- Output streams back live (not a final blob) and the remote task's exit code becomes your local process's exit code.
- For `capture`, produced artifacts (`screen.png`, `layout.*`) are pulled home automatically.

The client imposes no read-idle timeout on a valid execution response body:
a quiet compiler or running app must not trigger HTTPX's default five-second
read timeout. Response headers, HTTP error bodies, connection establishment,
writes, and pool acquisition retain five-second timeouts. There is no overall
client run deadline; interrupt the command when you want to stop waiting.
This does not disable timeouts imposed by a proxy or relay on the route.

Without `--remote`, nothing changes — `mship run/capture/build` behave exactly as before.

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

Each machine maps a role to a concrete `{url, token}` **locally**, in the gitignored `.mothership/run-hosts.yaml` (never in `mothership.yaml`):

```bash
# on the remote machine (the run host itself):
mship pair              # prints a groundcontrol://add?... link (+ QR) for its own relay URL + serve token

# on your operator machine:
mship run-host add ios-sim-host --pair-link 'groundcontrol://add?...'
# or, if you already have the url/token some other way:
mship run-host add ios-sim-host --url https://mac-abc123.relay.example.com --token <serve-token>

mship run-host list      # role -> url (tokens are always redacted)
mship run-host remove ios-sim-host
```

`MSHIP_RUN_HOST_<ROLE>_URL` / `MSHIP_RUN_HOST_<ROLE>_TOKEN` env vars override the file per-role, if you'd rather not persist a mapping (role upper-cased, `-` → `_`).

Because the mapping lives outside `mothership.yaml`, the same public config is portable across the whole team — each operator binds `ios-sim-host` to whatever Mac *they* have.

## Using it (`--remote[=role]`)

```bash
mship run --remote                 # bare: auto-resolve the role (repo's declared run_host, else the sole configured run_hosts entry)
mship run --remote=ios-sim-host    # explicit role
mship build --remote=android-emu-host
mship capture --repo ios-app --remote=ios-sim-host
```

`run`/`build --remote` require a resolvable task (`--task`, `MSHIP_TASK`, or cwd) — the remote checks out that task's branch, so there's no ad-hoc remote run. `capture --remote` has the same requirement (no ad-hoc remote capture).

Bare `--remote` (no `=role`) auto-resolves in this order: the target repo's declared `run_host`, else the sole entry in `run_hosts` if there's exactly one. Two or more roles with nothing chosen is an ambiguous-role error (see below).

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

## Known limitations (v1)

These are deliberately out of scope for the first cut. Know them before you lean on `--remote` for a repo with heavier setup needs.

- **`symlink_dirs` / `bind_files` are not replicated on the remote worktree.** `task setup` now runs there (see "Dependencies are derived there, not copied"), so a repo whose deps come from tracked manifests works. A repo that depends on symlinked gitignored material from your source checkout still does not.
- **Remote task stdout is streamed to your terminal verbatim.** There is no ANSI / control-sequence sanitization — the remote host is trusted. Don't point `--remote` at a host you don't control.
- **A `run_host:` set under a `capture:` block in `mothership.yaml` is silently ignored.** `CaptureConfig` has no `run_host` field; only the **repo-level** `run_host` (documented above under "Declaring roles") is honored. Put `run_host:` directly on the repo, not inside its `capture:` block.

## What travels to the run host

`--remote` runs the code you are looking at, **including work you have not
committed**. Before dispatching, mship reads every repo the task touches and
takes one of three paths per repo:

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

The run host accepts these pushes on a purpose-built endpoint (`/git/<repo>`)
that is bearer-authenticated with the same run-host token, accepts only repos
that workspace declares, and accepts writes only onto the `refs/mship/run/*`
namespace. It is not a mirror, not a remote you add by hand, and not a path for
real history. Each run force-updates its own ref, and `mship close` deletes the
task's scratch refs from the host.

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
