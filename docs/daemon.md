# The Mothership daemon (`mship daemon`)

One supervised daemon process per OS user per host, shipped from the same
package as the CLI. The daemon makes a host reachable/operable without a
terminal (#469); v1 (#470) provides provisioning, supervision, singleton-ness,
logs, and status, while the workspace registry (#472) discovers and serves
every healthy workspace under configured scan roots and the host tunnel (#471)
registers this machine with a relay and keeps an `ssh -R` up to it. It
supervises no workers (#473) yet; that remains a sibling capability behind the
seam reported by `mship daemon status`.

## Lifecycle

```bash
mship daemon install   # render + enable the OS-user unit, verify linger (Linux)
mship daemon start
mship daemon stop
mship daemon restart   # consults restart blockers first (the #473 recovery seam)
mship daemon status
mship daemon logs      # tails rotated logs + launchd stderr captures
mship daemon run       # foreground/debug, no supervisor
```

`mship serve` is unchanged: a foreground/API dev surface. Ordinary local mship
commands never require the daemon.

## Paths

- State: `~/.mothership/daemon/` (per OS user — the daemon is workspace-agnostic)
- Lease: `~/.mothership/daemon/daemon.lease` (flock held for the daemon's lifetime)
- Logs: `~/.mothership/daemon/logs/daemon.log` (rotated, 5MB x 3). Early-exit
  stderr (interpreter starts but dies before Python logging is configured) is
  captured by launchd into `launchd.*.log` in the same dir and included in
  `mship daemon logs`. A true pre-exec failure (missing executable) produces no
  child process at all, so NOTHING reaches these files: diagnose with
  `journalctl --user -u mship-daemon` on Linux or
  `launchctl print user/<uid>/com.mothership.daemon` / the unified log
  (`log show`) on macOS.
- Start history: `~/.mothership/daemon/start-history.json` (crash-loop visibility)
- Host identity: `~/.mothership/daemon/host-identity.json` (#471 — see
  [Tunnel registration](#tunnel-registration-471)), beside the credential stores
  `host-root-secret`, `host-tokens.json` and `host-refresh.json` (all 0600 in a
  0700 dir) and the tunnel's captured `ssh -R` output,
  `~/.mothership/daemon/logs/relay-tunnel.log`.
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

## Tunnel registration (#471)

With a relay configured, the daemon does two things beside the servers on the
same asyncio loop: it keeps an `ssh -R` tunnel to the relay up, and it keeps
this host's entry in the relay's host directory current. Together they are what
makes a freshly provisioned VM reachable from the phone with **no address typed
anywhere** — the phone scans one relay QR and reads the directory.

### Identity

Three distinct things, in `core/daemon/identity.py`:

- **`host_id`** — minted once (`hst-<ts>-<uuid8>`), persisted to
  `~/.mothership/daemon/host-identity.json`, and the name the relay's directory
  keys on. Deliberately *not* derived from the relay key: that key is a file,
  and a cloned VM reproduces it byte-for-byte.
- **machine fingerprint** — best-effort binding to the machine, read from the
  first readable of `/etc/machine-id`, `/var/lib/dbus/machine-id`,
  `/sys/class/dmi/id/product_uuid`, and recorded beside the `host_id`. It
  catches a *re-imaged* host. It does **not** catch a `cp -a`/snapshot clone,
  which copies it verbatim. An unreadable fingerprint (containers) reads as
  unknown, never as a mismatch — no false clone alarms.
- **`instance_id`** — minted per **process**, in memory, never written to disk.
  It is the one thing a clone cannot copy, so it is what lets the relay (and the
  tunnel's own read-back of its public `/health`) tell a restart from a second
  live claimant.

The relay subdomain is derived from the `host_id`, the relay key's device id and
the local subdomain secret — the same `<opaque-slug>-<6hex>` shape a serve
subdomain already has, so it needs no TLS/Caddy cert change.

**Re-identification** mints a new `host_id`, records the old one as
`cloned_from`, and **rotates the relay key**: the current key pair is moved
aside to `<name>.pre-reidentify-<UTC timestamp>` and a new one generated. The
rotation is the point — the clone still holds a copy of the old private key and
that key is still in the relay's `pubkeys/`, so keeping it would let the twin go
on authenticating as this host. The new key is unapproved by construction, so
the host lands back in the enrollment queue and needs one more
`mship relay approve <id>` on the relay box.

### Tokens

The phone's credential chain has two tiers, both self-issued by the host and
verified by that same host — nothing is proxied and no shared secret is
distributed:

- a **refresh credential** per `(host_id, client)`, *derived* (HMAC over the
  host root secret and a per-record nonce) rather than stored, so a
  reconnect/re-registration re-publishes the identical string and writes
  nothing at all. TTL 30 days; only `revoke` and a first mint touch the file.
  It is the field the directory entry carries.
- a **short-lived bearer**, `<token_id>.<secret>`, minted by
  `POST /host/token` in exchange for that refresh credential and good for
  `HOST_TOKEN_TTL_S` (300s). Only its sha256 is stored; verification is a pure
  read, so a reconnecting phone never churns the store.

Expiry is owned in one place (`core/relay/token_clock.py`) and is the **earlier**
of two bounds: an epoch-tagged monotonic deadline and the wall deadline plus a
120s skew grace. The monotonic floor is what makes an NTP step (or
`timedatectl set-time`) unable to silently extend a live bearer; the epoch tag
(the kernel boot id, or a per-process id where there is none) is what keeps a
floor taken in a previous boot from being compared against this boot's
`time.monotonic()`. Because the monotonic bound fires first for any ordinary
token, the skew grace only ever applies where the anchor cannot vouch for
elapsed time — a check after a restart, or a caller with no anchor.

Clock skew is *reported*, never gating: the link samples the enroll server's
`Date` header on every call and publishes `clock_skew_seconds`, which
`mship daemon status` prints once it exceeds a second.

### Provisioning

```bash
mship daemon install --serve 127.0.0.1:47190 --relay relay.example.com
mship daemon start
# then, once, on the relay box:
mship relay approve <id>
```

`--relay` needs a local bind to forward, from this install or an earlier one
(`--serve HOST:PORT`); like `--serve`, a **changed** relay takes effect on
`mship daemon restart`. Nothing else is manual: while its key is unapproved the
daemon posts `/enroll` non-blockingly and re-posts every 600s against the
relay's 1800s pending TTL, so a VM provisioned at 02:00 is still approvable at
09:00 with nobody at its terminal. Registration itself is challenge/response —
fetch a nonce (120s TTL), sign `namespace ‖ nonce ‖ canonical payload` with the
same ed25519 relay key the tunnel authenticates with (`ssh-keygen -Y sign`,
namespace `host-registration@mship`), POST it — repeated every 60s, and after a
failure on a 2s backoff doubling to a 60s cap, jittered **downward** so a fleet
coming back after a relay redeploy does not retry in lockstep.

`mship relay hosts` on the relay box lists the directory; a host that has not
re-registered within 240s (three intervals plus a worst-case backoff) reads as
`offline` there rather than disappearing.

### Tunnel state

There is no tunnel state file. The tunnel lives inside the daemon process, so
its published snapshot on `/health` is the only honest source — with no daemon
answering, `status` says so rather than guessing:

| state | what `mship daemon status` prints |
|---|---|
| `disabled` | `tunnel: disabled (no relay configured)` |
| `awaiting-enrollment` | `tunnel: awaiting relay approval (run 'mship relay approve <id>' on the relay host)` |
| `connecting` | `tunnel: connecting <public_url>` |
| `online` | `tunnel: online <public_url> (<n> restarts)` |
| `contended` | `tunnel: contended — another host holds <subdomain>` |
| `duplicate-identity` | `tunnel: rejected (duplicate-identity) — re-identifying automatically; 'mship daemon reidentify' to force` |
| `error` | `tunnel: error — <last_error>` |

(with no daemon running: `tunnel: unknown (daemon not running)`.)

`mship --json daemon status` carries the `/health` tunnel block verbatim under
`tunnel` — `state`, `subdomain`, `public_url`, `restarts`, `last_error`,
`clock_skew_seconds` — plus `clock_skew_seconds` at the top level, so a reader
never has to re-parse the prose.

`contended` outranks `error` deliberately: a live twin answering on our
subdomain is a specific, actionable fact, and the two co-occur constantly.

### Clone recovery

A `cp -a` clone booted beside its source publishes the same `host_id` on the
same subdomain, and the relay arbitrates by probing the incumbent's published
URL: the claim is refused only when the incumbent is still live and still
answers with its own `instance_id`. A stale entry is nobody's and is taken over
without a probe; an identical `(key fp, machine fp, instance_id)` re-post is the
same daemon reconnecting, not contention. The refused claimant gets
`409 duplicate-identity` and stops dialing — fighting a live twin for the
subdomain helps nobody.

- **Automatic.** After 3 consecutive 409s the link re-identifies itself — new
  `host_id`, rotated key — and drops back to `awaiting-enrollment`, re-posting
  `/enroll` at once. The clone therefore reappears in `GET /hosts` as
  `pending-approval` with no SSH session: the machine that needs this recovery
  is by definition one nobody can log into. It still needs approving.
- **Manual.** `mship daemon reidentify` forces the same move and prints the new
  host id and subdomain. `mship daemon reidentify --keep-identity` is the
  opposite claim — "this *is* the same host" — and adopts the running machine's
  fingerprint after a re-image or hardware change tripped the check, keeping the
  `host_id` and the key. After a forced re-identify, approve the new key and
  `mship daemon restart` to dial with it.

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

The tunnel half needs real sockets, a real relay and a second VM, which the
suite has none of: those items live in
[`checklists/host-tunnel-manual.md`](checklists/host-tunnel-manual.md).

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
  daemon scans **nothing** (never the whole filesystem). A configured root must
  be an accessible directory: install/start/refresh fail with that path rather
  than treating it as an empty scan, and an operational scan failure leaves the
  last registry state unchanged. A symlink supplied as a root is never traversed.
- Derived registry state is `~/.mothership/daemon/workspaces.json`.
- Without `--serve` the daemon is control-socket only: local `mship daemon ...`
  works, but the phone cannot reach it — and `--relay` refuses without a bind to
  forward. Bind a tailnet/LAN address here for direct reachability; the
  address-less path is [Tunnel registration](#tunnel-registration-471).

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
