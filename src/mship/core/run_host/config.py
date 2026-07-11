"""The concrete side of the two-layer run-host model.

`mothership.yaml` (public, committed) declares only logical role *names* via
`WorkspaceConfig.run_hosts` / `RepoConfig.run_host` — see
`mship.core.config`. Each machine then maps a role to a concrete connection
in the gitignored `.mothership/run-hosts.yaml` store (`RunHostStore`, in
`mship.core.run_host.store`). `RunHostConnection` is that connection's shape.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunHostConnection:
    """A resolved {url, token} for one run-host role.

    Mirrors `RelayConfig`'s role as a shape template (see
    `mship.core.relay.config.RelayConfig`), but this one is secret-bearing
    and therefore never lives in `mothership.yaml` — only in the per-machine
    store or an `MSHIP_RUN_HOST_*` env override.
    """

    url: str
    token: str
