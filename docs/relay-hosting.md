# Hosting Your Own mship Relay (sish)

This runbook walks through standing up a self-hosted [sish](https://github.com/antoniomika/sish) relay so that `mship serve --relay` can expose your local workspace over a stable `https://<workspace>.<relay-domain>` URL from anywhere — no VPN required.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| A VPS (Debian 12 or Ubuntu 22.04+) | 1 CPU / 512 MB RAM is sufficient. A $5/month cloud instance (Hetzner, DigitalOcean, Vultr, etc.) works. |
| A domain you control | Example: `relay.example.com`. You only need a subdomain — the relay does not take over the root domain. |
| SSH access to the VPS as root (or a sudo user) | To run the bootstrap script and open ports. |

---

## Step 1 — Point the Wildcard DNS Record at Your VPS

sish routes incoming HTTP/HTTPS traffic based on the `Host` header, so every workspace needs its own subdomain. A single wildcard `A` record covers all of them.

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
| 443 | TCP | HTTPS — public app traffic served to clients |

The compose file uses port **2222** for SSH (so it does not conflict with the host's own SSH daemon on 22). Allow it on your VPS firewall:

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
2. Creates the data directories `docker/relay/pubkeys/`, `docker/relay/keys/`, and `docker/relay/acme/`.
3. Prints a reminder to add client public keys before tunnels will be accepted.
4. Starts the sish container with `docker compose up -d`, passing `RELAY_DOMAIN` and `ACME_EMAIL` through to the compose file for the Let's Encrypt on-demand TLS certificate workflow.

The compose file (`docker/relay/docker-compose.yml`) mounts:

- `./pubkeys` → `/pubkeys` (read-only) — the SSH public-key allowlist.
- `./keys` → `/keys` — sish's own host key, persisted across container restarts.
- `./acme` → `/acme` — Let's Encrypt certificate cache.

---

## Step 4 — Add Client Public Keys

sish requires authentication: only clients whose public key appears in `docker/relay/pubkeys/` may open tunnels.

**On the machine running mship**, generate (or surface) the dedicated relay key:

```bash
mship relay setup
```

This prints a line like:

```
ssh-ed25519 AAAA... mship-relay
```

> Note: `mship relay setup` is implemented in Phase B of this feature. Until that command is available, run `ssh-keygen -t ed25519 -f ~/.mothership/relay_ed25519 -N "" -C "mship-relay"` manually and use the contents of `~/.mothership/relay_ed25519.pub`.

Copy that line to a file on the relay VPS:

```bash
# On the VPS, inside the mothership directory:
echo "ssh-ed25519 AAAA... mship-relay" > docker/relay/pubkeys/my-laptop.pub
```

One file per key; the filename does not matter. sish reads all files in the directory on each connection attempt — no container restart needed.

---

## Step 5 — Verify the Relay

Test that the relay is reachable and will accept a tunnel from an allowed key:

```bash
ssh -p 2222 \
    -o StrictHostKeyChecking=accept-new \
    -R test:80:localhost:8000 \
    relay.example.com
```

If everything is working you will see sish output similar to:

```
Starting SSH Forwarding service for http:80
```

And `https://test.relay.example.com` will proxy to `localhost:8000` on the machine that ran the command. Press `Ctrl-C` to close the tunnel.

Once this works, `mship serve --relay` will open and supervise tunnels automatically.

---

## Configuration Reference

The relay is configured entirely through environment variables passed to `docker compose`. The bootstrap script sets them; you can also export them in a `.env` file alongside `docker-compose.yml`:

| Variable | Required | Example | Description |
|---|---|---|---|
| `RELAY_DOMAIN` | Yes | `relay.example.com` | Base domain. Workspaces are exposed at `<subdomain>.<RELAY_DOMAIN>`. Must match the wildcard DNS record. |
| `ACME_EMAIL` | Yes | `you@example.com` | Email registered with Let's Encrypt for certificate expiry notifications. |

---

## Upgrading

sish uses a rolling `latest` tag. To update:

```bash
cd /path/to/mothership
docker compose -f docker/relay/docker-compose.yml pull
docker compose -f docker/relay/docker-compose.yml up -d
```

Data directories (`keys/`, `pubkeys/`, `acme/`) are mounted volumes and survive the upgrade.

---

## Troubleshooting

**Tunnel connection refused** — confirm port 2222 is open (`nc -zv relay.example.com 2222`) and the client public key is in `docker/relay/pubkeys/`.

**Certificate errors** — verify the wildcard DNS record resolves to the relay IP, and that port 80 is open (Let's Encrypt needs it for the HTTP-01 challenge).

**sish container exits immediately** — run `docker compose -f docker/relay/docker-compose.yml logs sish` to inspect startup errors. Common causes: port already in use, or missing `RELAY_DOMAIN`/`ACME_EMAIL` environment variables.
