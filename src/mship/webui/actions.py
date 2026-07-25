"""Status code -> the command that fixes it, pre-filled from the edge's facts.

Data, not logic: one entry per status code, each a template over `facts`. Kept
here rather than inline in the template so the mapping is testable, and kept
inside this package so it leaves with the frontend if the frontend is replaced.

The console SHOWS these; it never runs them. One serve bearer currently grants
approve + exec + gh-token (issue #370), so executing privileged mutations behind
that same bearer would turn it into a full admin credential reachable over the
relay. In-UI execution waits on scoped tokens.

The codes come from the topology layer's vocabulary. They are referenced as
plain strings — this package deliberately does not import the topology module
(that would couple the frontend to Python internals), so an unknown code simply
yields no card.
"""
from __future__ import annotations

#: code -> (label, command template). `{field}` slots are filled from
#: `edge["facts"]`.
_COMMANDS: dict[str, tuple[str, str]] = {
    "run_host_unmapped": (
        "Map this role on this machine",
        "mship run-host add {role} --pair-link '<paste from `mship pair` on that machine>'",
    ),
    "run_host_stale_token": (
        "Re-map with a fresh token",
        "mship run-host add {role} --pair-link '<paste a fresh link>'",
    ),
    "run_host_orphan_mapping": (
        "Drop the unused mapping",
        "mship run-host remove {role}",
    ),
    "run_host_not_bootstrapped": (
        "Bootstrap that machine",
        "mship bootstrap   # run on the remote, then restart `mship serve --relay` there",
    ),
    "run_host_unreachable": (
        "Check that machine is serving",
        "mship serve --relay   # run on the remote",
    ),
    "run_host_unknown_role": (
        "Declare the role, or fix the typo",
        "run_hosts: [{role}]   # add to mothership.yaml",
    ),
    "run_hosts_none_declared": (
        "Declare a role in mothership.yaml",
        "run_hosts: [<role-name>]   # add to mothership.yaml",
    ),
    "run_hosts_ambiguous_default": (
        "Name the role explicitly",
        "mship run --remote=<role>   # or declare `run_host: <role>` on the repo",
    ),
    "run_hosts_store_unreadable": (
        "Re-map roles after fixing the store",
        "mship run-host add <role>",
    ),
    "relay_not_configured": ("Start a relay serve", "mship serve --relay"),
    "relay_not_running": ("Restart the relay serve", "mship serve --relay"),
    "relay_unreachable": ("Restart the relay serve", "mship serve --relay"),
    "relay_no_public_url": ("Restart the relay serve", "mship serve --relay"),
    "relay_auth_failed": ("Re-pair this device", "mship pair"),
    "relay_subdomain_drift": ("Re-pair against the current subdomain", "mship pair"),
    "serve_relay_stale": ("Restart the relay serve", "mship serve --relay"),
    "serve_relay_absent": ("Start a relay serve", "mship serve --relay"),
    "gh_auth_none": (
        "Point this machine at a token broker",
        "export MSHIP_GH_BROKER_URL=<serve url> MSHIP_SERVE_TOKEN=<bearer>",
    ),
    "gh_auth_app_key_unreadable": (
        "Fix the App key path",
        "export MSHIP_GH_APP_KEY=/absolute/path/to/app.pem",
    ),
    "probe_skipped": ("Probe the network", "mship net status"),
}


def command_for(edge: dict) -> dict | None:
    """`{label, command}` for an unhealthy edge, or None when there is nothing to
    do (a healthy edge, or a code with no known remedy).

    Missing facts leave their placeholder visible rather than raising — a
    half-filled command is still better guidance than a blank card.
    """
    if edge.get("status") in ("ok", None):
        return None
    entry = _COMMANDS.get(edge.get("code", ""))
    if entry is None:
        return None
    label, template = entry
    facts = edge.get("facts") or {}
    try:
        command = template.format(**facts)
    except (KeyError, IndexError):
        command = template
    return {"label": label, "command": command}
