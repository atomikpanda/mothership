"""Read-only connectivity topology: what this machine is wired to, and whether
each edge is healthy.

ONE implementation, three thin callers — `mship net status`, `mship doctor`'s
connectivity group, and `GET /net/topology` on serve — so probe logic lives in
exactly one module.

Two invariants the callers depend on:

1. **Read-only.** Nothing here creates, writes, or rotates state. That rules out
   the `ensure_*` helpers in `relay.keys` and `relay.token` (they generate on
   absence) — this module reads their paths instead.
2. **Never raises.** A broken environment is the EXPECTED input; this tool is
   most needed exactly when connectivity is broken. Every failure becomes an
   edge status plus a fix hint, and every network probe is timeout-bounded.

`GET /net/topology`'s payload is the UI contract for the serve-host console, so
it carries `version` (SCHEMA_VERSION) and must be renderable on its own — no
companion in-process data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

#: Bound on every network probe. Short on purpose: `doctor` was fast before
#: connectivity checks existed, and an unreachable edge must not stall it.
PROBE_TIMEOUT_SECONDS = 3.0

#: `absent` = not configured on this machine (nothing to fix), distinct from
#: `fail` = configured but broken.
STATUSES = ("ok", "warn", "fail", "absent")

# --- status codes ---------------------------------------------------------
# One code per distinguishable state so a UI can branch without parsing prose.
# The prose lives in `Edge.detail` / `Edge.fix`.

SERVE_RELAY_RUNNING = "serve_relay_running"
SERVE_RELAY_STALE = "serve_relay_stale"
SERVE_RELAY_ABSENT = "serve_relay_absent"

RELAY_OK = "relay_ok"
RELAY_UNREACHABLE = "relay_unreachable"
RELAY_AUTH_FAILED = "relay_auth_failed"
RELAY_SUBDOMAIN_DRIFT = "relay_subdomain_drift"
RELAY_NOT_CONFIGURED = "relay_not_configured"
RELAY_NOT_RUNNING = "relay_not_running"

RUN_HOSTS_NONE_DECLARED = "run_hosts_none_declared"
RUN_HOSTS_AMBIGUOUS_DEFAULT = "run_hosts_ambiguous_default"
RUN_HOSTS_OK = "run_hosts_ok"
RUN_HOST_OK = "run_host_ok"
RUN_HOST_UNKNOWN_ROLE = "run_host_unknown_role"
RUN_HOST_UNMAPPED = "run_host_unmapped"
RUN_HOST_UNREACHABLE = "run_host_unreachable"
RUN_HOST_NOT_BOOTSTRAPPED = "run_host_not_bootstrapped"
RUN_HOST_STALE_TOKEN = "run_host_stale_token"
RUN_HOST_ORPHAN_MAPPING = "run_host_orphan_mapping"

GH_AUTH_APP = "gh_auth_app"
GH_AUTH_BROKER = "gh_auth_broker"
GH_AUTH_ENV_TOKEN = "gh_auth_env_token"
GH_AUTH_RELAY_ATTACH = "gh_auth_relay_attach"
GH_AUTH_NONE = "gh_auth_none"

EGRESS_ROUTED = "egress_routed"
EGRESS_ABSENT = "egress_absent"
EGRESS_UNKNOWN = "egress_unknown"


@dataclass(frozen=True)
class Edge:
    """One connectivity edge (or node) and its health.

    `facts` holds REDACTED, source-annotated values only — urls, hostnames,
    booleans, and where an effective value came from. Never a token, key, or
    credential body (see `tests/core/test_topology_redaction.py`).
    """
    kind: str          # serve | relay | run_host | gh_auth | egress
    name: str          # "serve", "relay", "run_host:mac-studio", ...
    status: str        # one of STATUSES
    code: str          # one of the module's status-code constants
    detail: str        # human summary
    fix: str | None    # actionable next step; None when there is nothing to fix
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Topology:
    version: int
    workspace: str
    probed_at: str     # ISO-8601, UTC
    edges: list[Edge]


def topology_payload(topology: Topology) -> dict[str, Any]:
    """The JSON body shared by `mship net status --json` and
    `GET /net/topology` — ONE serializer, so the CLI and the endpoint can never
    disagree about the contract."""
    return {
        "version": topology.version,
        "workspace": topology.workspace,
        "probed_at": topology.probed_at,
        "edges": [
            {
                "kind": e.kind,
                "name": e.name,
                "status": e.status,
                "code": e.code,
                "detail": e.detail,
                "fix": e.fix,
                "facts": dict(e.facts),
            }
            for e in topology.edges
        ],
    }
