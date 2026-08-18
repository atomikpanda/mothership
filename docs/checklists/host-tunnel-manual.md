# Manual checklist — host tunnel registration (#471)

The unit suite for #471 opens no sockets and takes no real sleeps: every
collaborator (`post`/`get`/`clock`/`rng`/`reaper`/`verify`/proc factory) is
injected, which is what makes the loops testable at all. That discipline is also
the honest boundary of what CI can prove here — everything below needs a real
`ssh -R`, a real relay, a second VM, a real clock or a real phone, and therefore
needs a human.

Run this pass on a real host + the live relay before calling #471 done.

## This environment

- **The workspace box IS the relay**: `mship-relay.atomikpanda.com`. There is no
  separate VPS to SSH into — "on the relay box" means here.
- **The compose project lives in the MAIN checkout**
  (`mship-workspace/mothership/docker/relay/`), never a worktree. A worktree's
  copy of `docker/relay/` has an empty `pubkeys/` (nothing is approved) and
  would generate a *different* sish host key, so bringing the stack up from a
  worktree breaks every existing tunnel. Always `docker compose -f
  <main checkout>/docker/relay/docker-compose.yml …`.
- **There is no passwordLESS sudo here** (interactive `sudo` at a terminal is
  fine — item 8 uses it; what is missing is unattended/scripted sudo). Where a
  step below wants a wedged peer, simulate it with `kill -STOP <pid of the ssh -R>` (and `kill -CONT` to
  release) rather than iptables/nft rules.
- Useful throughout: `mship daemon status` (and `mship --json daemon status`) on
  the host, `mship relay hosts --store-dir <relay-dir>/pending-store` on the
  relay, `~/.mothership/daemon/logs/relay-tunnel.log` for the ssh child's own
  output.

## Checklist

1. **Real tunnel over the live relay.** `mship daemon install --serve
   127.0.0.1:47190 --relay mship-relay.atomikpanda.com && mship daemon start`,
   then one `mship relay approve <id>` on the relay box → `mship daemon status`
   reaches `tunnel: online https://<subdomain>.mship-relay.atomikpanda.com`, and
   that URL answers `/health` from off-box.

2. **Re-registration after a tunnel flap (AC2 in reality).**
   `pkill -f 'ssh -R <subdomain>'` → the supervisor respawns the child and the
   daemon re-registers *once* (not once per tick): `last_seen` in
   `mship relay hosts` advances within seconds of the respawn, `restarts`
   increments in `mship --json daemon status`, and the public URL serves again.

3. **No orphan holds the subdomain.** `kill -9 <mshipd pid>` → on the next start
   the daemon reaps the reparented `ssh -R` (it survives the kill;
   `start_new_session=True`) and reaches `online` again on the same subdomain.
   Confirm with `pgrep -af 'ssh -R'` that exactly one ssh child exists
   afterwards.

4. **Clean shutdown (AC7 shutdown).** `systemctl --user stop mship-daemon` →
   the daemon exits within systemd's stop timeout with **no** `SIGKILL` in
   `journalctl --user -u mship-daemon` ("state 'stop-sigterm' timed out" must
   not appear), the ssh child is gone, and `start-history.json` gains a
   `clean_stop` entry (`mship daemon status` then reports no unclean start).

5. **Relay redeploy, no host action (AC3/AC13).** On the relay box,
   `docker compose … up -d --force-recreate sish` → every host's tunnel drops
   and every host is back `online` unattended (allow ~90s keepalive detection
   plus a jittered ≤60s backoff); no command is run on any host. Then
   `docker compose … up -d --force-recreate caddy` → `GET /hosts` on
   `https://enroll.mship-relay.atomikpanda.com` is reachable again (with the
   `Mship-Fleet-Token` header) — this is the check that would catch a lost
   `@hosts` matcher.

6. **Realistic VM clone (AC4).** `cp -a` a running host's disk and boot the copy
   **beside** its source **without truncating `/etc/machine-id`**. (Cloning from
   a prepared image that regenerates the machine id exercises the easy path —
   the local fingerprint check — and would pass while the real case fails.)
   Expect: the source stays `online` and keeps the subdomain; the clone reports
   `tunnel: rejected (duplicate-identity)`, and after 3 consecutive refusals
   re-identifies itself automatically (new `host_id`, rotated relay key) and
   re-posts `/enroll`, so it shows up in `mship relay hosts` as a
   **`pending-approval` row under a new key fingerprint** — with nobody logging
   into it. (Pending rows come from the enroll store, so their `host_id` is
   null until the first successful registration.) Approving that new key brings
   the clone up as its own host on its own subdomain, and the clone's daemon log
   carries the "re-identified as hst-…" warning.

7. **Reboot + linger survival.** `loginctl enable-linger`, reboot the host, do
   not log in → the daemon is running and its tunnel is `online` again, verified
   from another machine.

8. **Wall-clock step (AC10).** `sudo timedatectl set-time '+1 hour'` (interactive
   sudo is fine here — it is passwordLESS sudo this box does not have), or step
   the VM's clock, while a phone is paired.

   A *forwards* step past `expires_at + 120s` **rejects** the bearers minted
   before it — that is the intended behaviour, not a failure: the wall bound and
   the monotonic bound are ANDed as "earliest wins", and the monotonic floor
   exists to stop a *backwards* step silently extending a live token, not to
   keep a stale one alive. What must not happen is the phone falling out of
   pairing over it.

   **PASS =** the phone keeps working with no re-pairing — it silently re-mints
   a bearer via `POST /host/token` from its 30-day refresh credential, which a
   one-hour step cannot expire — **and** `mship --json daemon status` reports a
   non-zero `clock_skew_seconds` (sampled from the enroll server's `Date`, so it
   is measured against the relay, not against itself). Put the clock back
   afterwards.

9. **Worker survives a tunnel outage (AC7).** Start a long-running task on the
   host, then take the tunnel away for ~10 minutes — `kill -STOP` the `ssh -R`
   pid (no sudo needed; do **not** reach for iptables here) or stop sish on the
   relay. After `kill -CONT` / restarting sish: the task is still running, the
   daemon reconnects on its own, and its state is visible again from the phone.
   Nothing about the outage may kill work in progress.

10. **Phone end-to-end from one QR (AC1).** On the relay box,
    `mship relay fleet-token --label phone --relay-domain
    mship-relay.atomikpanda.com --store-dir <relay-dir>/pending-store`; scan the
    printed QR once in Ground Control. **No address is typed anywhere.** A
    freshly provisioned host appears by itself once approved.

11. **Two hosts at once (AC5).** With a second host registered, both are visible
    simultaneously in the app from that same single pairing, each addressable
    on its own subdomain.

12. **Relay down, host up (AC9).** Stop the relay stack (or block it) while a
    workspace is still reachable on the LAN/tailnet → the phone still opens that
    workspace over its direct address. The relay is a convenience path, not a
    dependency for a host you can already reach.
