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
2. **The remote's own git credentials** — the remote box fetches the task branch using its own git auth (a normal git credential helper, SSH key, or the Phase-1 GitHub token broker for a credential-less/cloud remote). No GitHub token ever crosses the wire between your box and the remote.

Nothing secret is ever committed to `mothership.yaml` — that file only ever holds role *names*.

## Where capture artifacts land

`mship capture --remote` writes artifacts to the exact same local path a local capture would use:

```
.mothership/captures/<task-slug|_adhoc>/<UTCts>-<platform>/
```

so `discover_artifacts` and anything reading captures locally (including an agent) sees them unchanged, regardless of whether the capture ran locally or on a remote host.

## Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `unknown run-host role '<role>'; not declared in this workspace's \`run_hosts:\` list` | You passed `--remote=<role>` (or a repo declared `run_host: <role>`) but that name isn't in `mothership.yaml`'s `run_hosts:` list — likely a typo. | Add the role to `run_hosts:` in `mothership.yaml`, or fix the typo. |
| `ambiguous run-host: multiple roles are configured (...) and none was specified` | Bare `--remote` with 2+ roles in `run_hosts:` and no repo-declared default. | Pass `--remote=<role>` explicitly, or declare `run_host: <role>` on the repo. |
| `run-host role '<role>' is declared but has no connection mapped on this machine; run \`mship run-host add <role>\`` | The role exists in `mothership.yaml`, but *this* machine never mapped it to a `{url, token}`. | `mship run-host add <role> --pair-link '...'` (get the link by running `mship pair` on the remote). |
| `remote host at <url> is unreachable via relay (...)` | Couldn't even connect — the remote isn't running `mship serve --relay`, the relay is down, or the pairing is stale. | Confirm the remote is up and `mship serve --relay` is running there; re-pair if the relay subdomain changed. |
| `remote workspace not bootstrapped at <url> (503)` | The remote's `mship serve --relay` is reachable, but that machine has no workspace config wired in (no `mothership.yaml`, or serve was started without one). | Bootstrap that machine as an mship workspace and restart `mship serve --relay` there. |
| `remote host at <url> rejected the bearer token (401)` | The mapped token is wrong or was rotated on the remote. | Re-run `mship run-host add <role>` with a fresh pair link/token. |
| `error: unknown repo(s) ...; known repos: ...` (streamed, then a non-zero exit) | The remote's own `mothership.yaml` doesn't have a repo of that name — usually a workspace mismatch between your box and the remote. | Confirm both workspaces declare the same repo names, or pass `--repos` naming a repo the remote actually has. |
| A repo's task lines print, then `error: branch-materialize failed for repo '<repo>': ...` (then a non-zero exit) | The remote's `git fetch`/`git worktree add` for that repo's task branch failed — commonly the branch not pushed yet, or a dirty/locked worktree on the remote. | Push the task's branch, or clear the stuck worktree on the remote (`git worktree remove`/`prune`), then retry. |
| Remote task's own output ends with a non-zero `__MSHIP_EXIT__ <code>` | The task itself failed on the remote — same as a local failure. The streamed output above the exit line is the task's real stdout/stderr. | Read the streamed output like any other failing `run`/`build`/`capture`. |
| `--remote requires a resolvable task: ...` / `--remote requires an active task: ...` | You ran `--remote` with no active/resolvable task. Remote execution always needs a branch to check out. | Pass `--task <slug>`, or run from inside an active task's worktree. |
