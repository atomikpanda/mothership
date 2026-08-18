# The Mothership daemon (`mship daemon`)

One supervised daemon process per OS user per host, shipped from the same
package as the CLI. The daemon makes a host reachable/operable without a
terminal (#469); v1 (#470) is the lifecycle substrate: provisioning,
supervision, singleton-ness, logs, and status. It serves no workspaces (#472),
opens no tunnel (#471), and supervises no workers (#473) yet — those arrive as
sibling capabilities behind the seams reported by `mship daemon status`.

## Lifecycle

```bash
mship daemon install   # render + enable the OS-user unit, verify linger (Linux)
mship daemon start
mship daemon stop
mship daemon restart   # consults restart blockers first (the #473 recovery seam)
mship daemon status
mship daemon logs      # tails rotated logs, no journald needed
mship daemon run       # foreground/debug, no supervisor
```

`mship serve` is unchanged: a foreground/API dev surface. Ordinary local mship
commands never require the daemon.

## Paths

- State: `~/.mothership/daemon/` (per OS user — the daemon is workspace-agnostic)
- Lease: `~/.mothership/daemon/daemon.lease` (flock held for the daemon's lifetime)
- Logs: `~/.mothership/daemon/logs/daemon.log` (rotated, 5MB x 3)
- Start history: `~/.mothership/daemon/start-history.json` (crash-loop visibility)
- Control socket: `$XDG_RUNTIME_DIR/mship/daemon.sock`, else
  `~/.mothership/daemon/run/daemon.sock`. Status probes prefer the socket path
  recorded in the lease (the daemon's env and your shell can disagree about
  `XDG_RUNTIME_DIR`).

## Why linger is mandatory (Linux)

A `systemd --user` unit is torn down when the user's last session ends —
precisely the moment a headless daemon is needed. `mship daemon install` runs
`loginctl enable-linger` and verifies `Linger=yes`, and `status` re-warns
whenever linger is off.

## Loser-exits-0 policy

Two `mshipd` processes can race (supervisor + `daemon run`, or concurrent cold
starts). The lease flock decides the winner; the loser probes the holder's
control socket:

- holder answers `/health` → the loser exits **0**. launchd's
  `KeepAlive.SuccessfulExit=false` relaunches on *any* nonzero exit every
  `ThrottleInterval` — a permanent hot loop — so 0 is the only supervisor-safe
  loser status on both OSes. The log line naming the holder pid is the
  diagnostic.
- holder never answers → exit **1** (contended-but-dead) so the supervisor
  retries rather than parking "inactive-success" with zero daemons.

The held flock is the liveness authority; the recorded pid is diagnostic only
(pid reuse must never read as "already running"). No CLI/status path ever
touches the lease flock.

## Upgrades

Merging does not deploy (same reality as `redeploy-serve.sh`): a running daemon
keeps executing the version it imported at start. Deploy =
`uv tool install --force --no-cache <path>` then `mship daemon restart`.
`status` shows "restart required: daemon vA, CLI vB" whenever the running
daemon's version differs from the CLI's (exact-match policy; CI bumps a patch
per merge).

## macOS caveats

- The LaunchAgent is bootstrapped in the `user/<uid>` domain so provisioning
  works over SSH with no GUI session (`gui/<uid>` fails there with
  "Bootstrap failed: 5: Input/output error").
- Reboot-survival on a headless Mac requires a login session (enable
  auto-login); a system-domain LaunchDaemon is out of scope for v1.

## Manual/VM verification checklist

The suite fakes the supervisor boundary; these OS-contract behaviors need a
real VM pass:

1. **Linger:** `mship daemon install && mship daemon start`, close every SSH
   session, wait 60s, reconnect → daemon still running (`mship daemon status`).
2. **Crash recovery:** `kill -9 <pid>` → daemon back within ~5s
   (`RestartSec=5`); `status` shows an unclean start.
3. **Reboot:** reboot the host, no SSH login → daemon running (verify from a
   second host or after login; the point is it started without one).
4. **Headless macOS:** `mship daemon install` over SSH with no GUI session
   succeeds (user-domain bootstrap).
5. **Crash loop:** make `mshipd` exit nonzero immediately (e.g. temporarily
   break the venv) → systemd reaches `start-limit-hit` within ~5 failures and
   `mship daemon status` shows the unclean-start count + `failed` state.
6. **Upgrade:** `uv tool install --force --no-cache <new>` → `status` shows
   "restart required" → `mship daemon restart` → `status` clean, new version
   reported.
7. **Concurrent cold start:** run `mship daemon start` while `mship daemon run`
   is already active in a shell → exactly one daemon survives; the loser logs
   the holder pid and exits 0.

## Workspaces (#472)

The daemon discovers workspaces instead of being told about them: at startup
(and on explicit refresh) it scans the configured **scan roots** for
`mothership.yaml` and serves every healthy one.

```bash
mship daemon install --scan-root ~/src --scan-root ~/work --serve 127.0.0.1:47190
```

- Scan roots and the optional TCP bind live in `~/.mothership/daemon/config.yaml`;
  edit scan roots and run `mship workspace refresh` to pick them up. Changes to
  the `serve:` bind require `mship daemon restart`. With no roots configured the
  daemon scans **nothing** (never the whole filesystem).
- Derived registry state is `~/.mothership/daemon/workspaces.json`.
- Without `--serve` the daemon is control-socket only: local `mship daemon ...`
  works, but the phone cannot reach it. Address-less reachability is #471; until
  then bind a tailnet/LAN address here.

### Addressing

Every workspace operation names its workspace by **id**:

```
GET  /workspaces                      # list: id, name, path, state, repos, runtime
POST /workspaces/refresh              # rescan + reconcile
GET  /workspaces/<id>/specs           # ... and every other serve route
```

Ids are minted (`ws-<ts>-<rand>`) and persisted to
`<workspace>/.mothership/workspace-id`, never derived from the directory name —
two workspaces may share a basename *and* a display name. Moving a workspace
keeps its id (the id file travels with it); deleting one leaves a visible
`missing` entry rather than a phantom. A **copy** (`cp -r`, cloned VM image)
carries the same id file: the original keeps the id and the copy appears as a
`degraded` duplicate-identity entry — run `mship workspace add <copy>` to mint
it a fresh id.

`mship workspace list|add|remove|ignore|refresh` are override/inspection
controls, not the onboarding path: `add` is for a workspace discovery can't
reach (outside every scan root) or to promote a duplicate copy.

### What is never a workspace

`.worktrees/` and `.mothership/` are mandatory scan exclusions, and a directory
whose `.git` is a *file* (a linked worktree's gitdir pointer) or that sits under
a `.mship-workspace` marker is a task worktree, not a workspace — `mship spawn`
materializes a full tracked `mothership.yaml` inside each worktree, so without
these rules every spawned task would register as a phantom workspace. Nested
markers resolve to the outermost workspace, so a repo inside a metarepo never
registers separately.

### Degraded entries

A broken/unreadable `mothership.yaml` — or a valid one whose repo paths don't
exist (a template like `examples/mothership.yaml`) — becomes a visible
`degraded` entry carrying the reason. Siblings still discover, the scan never
aborts, and requests to a degraded id return 503 with that reason rather than
failing obscurely at dispatch time.

### Ground Control

Pair a **host** once using its base URL and effective host token. A non-empty
`MSHIP_SERVE_TOKEN` in the daemon process takes precedence over
`~/.mothership/daemon/serve-token`; when the environment override is unset, pair
with the token from that file. "Discover workspaces on host" then lists what
that host found; picking one stores a connection pointed at
`{host}/workspaces/{id}`. If you previously paired that same workspace by hand
(old per-workspace URL), you'll see two cards until you remove the manual one —
migration lands with #471.

`mship daemon install` and `mship daemon start` persist a non-empty shell
override to that owner-only file before handing off to the supervisor, so the
same effective token survives launchd/systemd startup.

### Multiple hosts

The registry is **per host**. The same workspace discovered on two hosts is two
independent entries, and nothing in the registry claims exclusive ownership:
which host actually runs a WorkItem is decided by the claim protocol (#473).
