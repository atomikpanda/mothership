# Hosting Your Own mship Relay (sish + Caddy)

This runbook walks through standing up a self-hosted [sish](https://github.com/antoniomika/sish) relay so that `mship serve --relay` can expose your local workspace over a stable `https://<workspace>.<relay-domain>` URL from anywhere — no VPN required.

**Architecture overview.** Caddy is the public web front (ports 80 and 443). It terminates TLS using Let's Encrypt **on-demand** TLS, gated by a `ask` endpoint so certificates are only issued for `enroll.<relay>` and per-device serve subdomains (`<slug>-<6hex>.<relay>`). sish runs behind Caddy — it owns SSH (`:2222`) and serves HTTP internally on `127.0.0.1:8080`; it never sees TLS. The enroll-server binds loopback (`127.0.0.1:47180`) and is reached exclusively through Caddy; no external firewall hole is needed for it.

```
internet
  │
  ├─ :2222 (TCP) ───────────────────────────────────── sish (SSH tunnels)
  │
  └─ :80 / :443 (TCP) ──── Caddy (TLS termination)
                               │
                               ├─ enroll.<relay> ──── enroll-server (127.0.0.1:47180)
                               │
                               └─ *.<relay> ──────── sish HTTP (127.0.0.1:8080)
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| A VPS (Debian 12 or Ubuntu 22.04+) | 1 CPU / 512 MB RAM is sufficient. A $5/month cloud instance (Hetzner, DigitalOcean, Vultr, etc.) works. |
| A domain you control | Example: `relay.example.com`. You only need a subdomain — the relay does not take over the root domain. |
| SSH access to the VPS as root (or a sudo user) | To run the bootstrap script and open ports. |

---

## Step 1 — Point the Wildcard DNS Record at Your VPS

Caddy routes incoming HTTPS traffic based on the `Host` header, so every workspace needs its own subdomain. A single wildcard `A` record covers all of them (including `enroll.<relay>`).

In your DNS provider, create:

```
*.relay.example.com   A   <public-ip-of-your-vps>   TTL 300
```

Replace `relay.example.com` with your actual relay base domain and `<public-ip-of-your-vps>` with the server's IPv4 address.

Verify propagation before continuing:

```bash
dig +short A test.relay.example.com
# Should return your VPS IP
```

---

## Step 2 — Open Firewall Ports

The relay needs three TCP ports reachable from the public internet:

| Port | Protocol | Purpose |
|---|---|---|
| 22 **or** 2222 | TCP | SSH — mship clients open reverse tunnels here |
| 80 | TCP | HTTP — ACME (Let's Encrypt) HTTP-01 challenge + HTTP→HTTPS redirect |
| 443 | TCP | HTTPS — public app traffic and enroll endpoint (TLS terminated by Caddy) |

The compose file uses port **2222** for SSH (so it does not conflict with the host's own SSH daemon on 22). The enroll-server port `:47180` is loopback-only and does **not** need an external firewall rule.

```bash
# ufw (Ubuntu/Debian)
ufw allow 2222/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw reload
```

If your cloud provider has a separate security group / network firewall, add the same rules there.

---

## Step 3 — Run the Bootstrap Script

Clone (or copy) the mothership repo to your VPS, then run the one-shot bootstrap:

```bash
git clone https://github.com/your-org/mothership.git
cd mothership

RELAY_DOMAIN=relay.example.com \
ACME_EMAIL=you@example.com \
./scripts/relay-bootstrap.sh
```

What the script does:

1. Installs Docker Engine if it is not already present (via `https://get.docker.com`).
2. Creates the data directories `docker/relay/pubkeys/`, `docker/relay/keys/`, `docker/relay/caddy-data/`, and `docker/relay/caddy-config/`.
3. Prints a reminder to add client public keys before tunnels will be accepted.
4. Starts the sish and Caddy containers with `docker compose up -d`.

The compose file (`docker/relay/docker-compose.yml`) starts two services:

- **sish** — `--https=false`, HTTP on internal `127.0.0.1:8080`, SSH on `:2222`. Mounts `./pubkeys` (read-only key allowlist) and `./keys` (sish host key).
- **caddy** — `network_mode: host` so it can bind `:80`/`:443` directly and reach the sish and enroll-server loopback addresses. Mounts `./Caddyfile`, `./caddy-data`, and `./caddy-config`.

The `Caddyfile` (`docker/relay/Caddyfile`) wires:

- `enroll.{$RELAY_DOMAIN}` → `127.0.0.1:47180` (enroll-server; only `POST /enroll` and `GET /status/*` are forwarded — everything else returns 404).
- `*.{$RELAY_DOMAIN}` → `127.0.0.1:8080` (sish HTTP, `Host` header preserved).

On-demand TLS is gated by the enroll-server's `/tls-check` ask endpoint, so Caddy only issues certificates for known hostnames.

---

## Step 4 — Start the Enroll Server

The enroll-server is not part of the Docker compose stack — it runs as a long-lived process on the relay host (e.g. under systemd or tmux):

```bash
mship relay enroll-server \
  --relay-domain relay.example.com \
  --store-dir /path/to/docker/relay/enroll-store \
  --pubkeys-dir /path/to/docker/relay/pubkeys
# Binds 127.0.0.1:47180 by default; Caddy proxies public traffic to it.
# pending requests expire after 30 min.
```

`--relay-domain` can also be set via the `RELAY_DOMAIN` environment variable. The enroll-server is reached from the internet only through Caddy at `https://enroll.<relay>` — the raw `:47180` port is not accessible externally.

> **Important — keep the enroll-server supervised.** The enroll-server backs Caddy's on-demand TLS `ask` endpoint, which gates cert issuance **and renewal** for every relay subdomain — not just new enrollment requests. If the enroll-server is down, Caddy cannot renew existing certs and will refuse to issue new ones for serve subdomains. Unlike sish and Caddy (which Docker Compose restarts automatically), the enroll-server runs outside the compose stack and **must run under a supervisor so it survives reboots**.
>
> The bootstrap script installs a systemd unit automatically when run as root. To install it manually:
>
> ```ini
> # /etc/systemd/system/mship-relay-enroll.service
> [Unit]
> Description=mship relay enroll-server (device enrollment + Caddy on-demand TLS ask)
> After=network-online.target docker.service
> Wants=network-online.target
>
> [Service]
> ExecStart=<MSHIP_BIN> relay enroll-server --relay-domain <RELAY_DOMAIN> --pubkeys-dir <relay-dir>/pubkeys --store-dir <relay-dir>/pending-store
> Restart=always
> RestartSec=2
>
> [Install]
> WantedBy=multi-user.target
> ```
>
> Enable and start it:
>
> ```bash
> systemctl daemon-reload
> systemctl enable --now mship-relay-enroll.service
> ```

---

## Step 5 — Add Client Public Keys

sish requires authentication: only clients whose public key appears in `docker/relay/pubkeys/` may open tunnels.

**On the machine running mship**, generate (or surface) the dedicated relay key:

```bash
mship relay setup
```

This prints a line like:

```
ssh-ed25519 AAAA... mship-relay
```

> Note: until `mship relay setup` is available, run `ssh-keygen -t ed25519 -f ~/.mothership/relay_ed25519 -N "" -C "mship-relay"` manually and use the contents of `~/.mothership/relay_ed25519.pub`.

Copy that line to a file on the relay VPS:

```bash
# On the VPS, inside the mothership directory:
echo "ssh-ed25519 AAAA... mship-relay" > docker/relay/pubkeys/my-laptop.pub
```

One file per key; the filename does not matter. sish reads all files in the directory on each connection attempt — no container restart needed.

### Enrolling a device that can't reach the relay box

When a *new device* (a laptop you can't SSH into the relay box from) needs access, use the **request → approve** flow — no shared secret, and a request can never enroll itself:

**On the new device**, request access using the relay hostname (not a full URL):

```bash
mship relay enroll --relay-host relay.example.com
# Derives https://enroll.relay.example.com automatically.
# Prints: "requested (id a1c2…); waiting for owner approval…"
# Polls and prints "approved" once the owner approves.
```

You can also pass an explicit URL if you need to override the derived address:

```bash
mship relay enroll --enroll-url https://enroll.relay.example.com
```

**Back on the relay host**, review and grant (or deny):

```bash
mship relay requests                 # id · hostname · key fingerprint
mship relay approve a1c2 \
  --store-dir docker/relay/enroll-store \
  --pubkeys-dir docker/relay/pubkeys
# writes the key into pubkeys/ → sish picks it up (no restart needed).
# `mship relay deny <id>` discards it.
```

A request only ever creates a *pending* entry — nothing reaches the allowlist until you `approve` it, and pending requests auto-expire after 30 minutes. The device can then `mship serve --relay`.

---

## Step 6 — Manual Smoke Test

After the stack is running, verify each layer:

1. **Containers up**: `docker compose -f docker/relay/docker-compose.yml ps` — both `sish` and `caddy` should be `Up`.

2. **SSH reachable**: `nc -zv relay.example.com 2222` should connect.

3. **Enroll server visible through Caddy**: from *another device*, run:
   ```bash
   mship relay enroll --relay-host relay.example.com
   ```
   It should print a request ID and enter polling. `:47180` should **not** be reachable directly from outside the relay host.

4. **Approve and confirm**: on the relay host, `mship relay approve <id>` — the device should print `approved`.

5. **Serve subdomain**: run `mship serve --relay` from an enrolled device and confirm the printed URL (`https://<slug>-<6hex>.relay.example.com`) loads through Caddy (look for a valid TLS certificate issued by Let's Encrypt).

6. **Port 47180 closed externally**: `nc -zv relay.example.com 47180` should time out or refuse — it is loopback-only.

---

## Configuration Reference

The relay is configured entirely through environment variables passed to `docker compose`. The bootstrap script sets them; you can also export them in a `.env` file alongside `docker-compose.yml`:

| Variable | Required | Example | Description |
|---|---|---|---|
| `RELAY_DOMAIN` | Yes | `relay.example.com` | Base domain. Workspaces are exposed at `<subdomain>.<RELAY_DOMAIN>`. Must match the wildcard DNS record. |
| `ACME_EMAIL` | Yes | `you@example.com` | Email registered with Let's Encrypt for certificate expiry notifications. |

The `mship relay enroll-server` command also respects `RELAY_DOMAIN` if `--relay-domain` is not passed explicitly.

---

## Deferred / Future Work

- **Rate limiting on the enroll endpoint** — a custom Caddy image with a rate-limit plugin will protect `POST /enroll` against abuse. The current `request_body max_size 4KB` limit is a lightweight guard only.
- **Wildcard DNS-01 TLS as an alternative** — for providers that support a Caddy ACME DNS plugin, a single wildcard certificate (`*.relay.example.com`) via DNS-01 challenge is a cleaner TLS strategy and avoids per-subdomain on-demand issuance.

---

## Upgrading

sish and Caddy both use the `latest` tag. To update:

```bash
cd /path/to/mothership
docker compose -f docker/relay/docker-compose.yml pull
docker compose -f docker/relay/docker-compose.yml up -d
```

Data directories (`keys/`, `pubkeys/`, `caddy-data/`, `caddy-config/`) are mounted volumes and survive the upgrade.

---

## Troubleshooting

**Tunnel connection refused** — confirm port 2222 is open (`nc -zv relay.example.com 2222`) and the client public key is in `docker/relay/pubkeys/`.

**Certificate errors** — verify the wildcard DNS record resolves to the relay IP, and that ports 80 and 443 are open. Caddy writes ACME state to `docker/relay/caddy-data/`; check `docker compose logs caddy` for ACME errors.

**sish container exits immediately** — run `docker compose -f docker/relay/docker-compose.yml logs sish` to inspect startup errors. Common causes: port already in use, or missing `RELAY_DOMAIN` environment variable.

**Caddy container exits immediately** — check `docker compose logs caddy`. A malformed `Caddyfile` or a missing `RELAY_DOMAIN`/`ACME_EMAIL` variable is the usual cause.

**Enroll request times out** — confirm the enroll-server process is running on the relay host (`curl http://127.0.0.1:47180/status/x` from the host should return a JSON status). Also check that Caddy is running and that `enroll.<relay>` resolves to the relay IP.
