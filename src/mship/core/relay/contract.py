"""Single source of truth for the relay egress contract (spec relay-aware-worker-boot).

Both ends of the relay must agree on:
  - the worker-facing path prefixes and the upstream host each maps to
    (/gh/ -> github.com git, /api/ -> api.github.com REST), and
  - the header name the worker carries its low-value per-run token in.

The egress-proxy PARSES incoming requests by these prefixes (egress/request.py)
and reads the run token from this header (egress/proxy.py); the worker-side git
config (worker_config.py) and the relay preflight probe (gh_preflight.py) EMIT
exactly the same prefixes + header. Sourcing both ends here means they can
never drift.
"""
from __future__ import annotations

GH_PREFIX = "/gh/"
API_PREFIX = "/api/"
GH_HOST = "github.com"
API_HOST = "api.github.com"

# Worker-facing path prefix -> upstream host. Adding a host = one entry here
# + one route (egress/routes.py) + one tls_ask/Caddy allowance.
PREFIX_HOST: dict[str, str] = {GH_PREFIX: GH_HOST, API_PREFIX: API_HOST}

# Header the worker carries its per-run token in (relay attaches real creds at egress).
RUN_TOKEN_HEADER = "Mship-Run-Token"
