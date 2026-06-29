#!/usr/bin/env bash
# One-shot bring-up of a self-hosted sish relay on a fresh Debian/Ubuntu VPS.
# Usage: RELAY_DOMAIN=relay.example.com ACME_EMAIL=you@example.com ./relay-bootstrap.sh
set -euo pipefail
: "${RELAY_DOMAIN:?set RELAY_DOMAIN (the wildcard base, e.g. relay.example.com)}"
: "${ACME_EMAIL:?set ACME_EMAIL (for Lets Encrypt / ACME)}"

if ! command -v docker >/dev/null 2>&1; then
  echo "[bootstrap] installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
HERE="$(cd "$(dirname "$0")/../docker/relay" && pwd)"
mkdir -p "$HERE/pubkeys" "$HERE/keys" "$HERE/caddy-data" "$HERE/caddy-config"
echo "[bootstrap] add client public keys to $HERE/pubkeys/ (one file per key), then:"
echo "  RELAY_DOMAIN=$RELAY_DOMAIN ACME_EMAIL=$ACME_EMAIL docker compose -f $HERE/docker-compose.yml up -d"
echo "[bootstrap] DNS: point  *.$RELAY_DOMAIN  A record at this host's public IP."
RELAY_DOMAIN="$RELAY_DOMAIN" ACME_EMAIL="$ACME_EMAIL" docker compose -f "$HERE/docker-compose.yml" up -d
# Supervise the enroll-server: it backs Caddy's on-demand TLS `ask`, so it gates
# cert issuance/renewal for EVERY relay subdomain, not just enrollment — keep it up.
# Run it as the user who owns the relay dir (the operator who also runs
# `mship relay approve`), so the pending store stays readable by that user — not root.
RELAY_USER="$(stat -c '%U' "$HERE")"
# Locate mship in the relay user's login PATH (handles a ~/.local/bin user install),
# falling back to root's PATH.
MSHIP_BIN="$(sudo -u "$RELAY_USER" sh -lc 'command -v mship' 2>/dev/null || command -v mship 2>/dev/null || true)"
if [ "$(id -u)" = 0 ] && command -v systemctl >/dev/null 2>&1 && [ -n "$MSHIP_BIN" ]; then
  cat >/etc/systemd/system/mship-relay-enroll.service <<UNIT
[Unit]
Description=mship relay enroll-server (device enrollment + Caddy on-demand TLS ask)
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=$RELAY_USER
ExecStart=$MSHIP_BIN relay enroll-server --relay-domain $RELAY_DOMAIN --pubkeys-dir $HERE/pubkeys --store-dir $HERE/pending-store
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now mship-relay-enroll.service
  echo "[bootstrap] enroll-server supervised via systemd as user '$RELAY_USER' (mship-relay-enroll.service)."
else
  echo "[bootstrap] IMPORTANT: supervise the enroll-server — it backs Caddy's on-demand TLS ask"
  echo "[bootstrap]   (gates ALL relay cert issuance/renewal). Run it under systemd or equivalent:"
  echo "[bootstrap]   RELAY_DOMAIN=$RELAY_DOMAIN mship relay enroll-server --pubkeys-dir $HERE/pubkeys --store-dir $HERE/pending-store"
fi
