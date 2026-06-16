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
mkdir -p "$HERE/pubkeys" "$HERE/keys" "$HERE/acme"
echo "[bootstrap] add client public keys to $HERE/pubkeys/ (one file per key), then:"
echo "  RELAY_DOMAIN=$RELAY_DOMAIN ACME_EMAIL=$ACME_EMAIL docker compose -f $HERE/docker-compose.yml up -d"
echo "[bootstrap] DNS: point  *.$RELAY_DOMAIN  A record at this host's public IP."
RELAY_DOMAIN="$RELAY_DOMAIN" ACME_EMAIL="$ACME_EMAIL" docker compose -f "$HERE/docker-compose.yml" up -d
echo "[bootstrap] sish is up. Test a tunnel: ssh -p 2222 -R myws:80:localhost:8000 $RELAY_DOMAIN"
