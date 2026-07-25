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

import shlex

#: Tokens that stand in for a value the console CANNOT know — a pair link is a
#: secret minted by `mship pair` on another machine, a bearer belongs to a serve
#: this host may never have talked to. A card containing any of these is marked
#: `needs_input` so the UI presents it as "edit, then run" rather than as a
#: turnkey command that would die in an argument parser (Greptile, PR #412).
#:
#: The invariant this buys, enforced in tests: every card either runs unchanged
#: OR is flagged `needs_input`. "Every card runs unchanged" is not achievable —
#: some remediations genuinely require operator-supplied secrets.
_PLACEHOLDERS = (
    "PAIR_LINK", "ROLE_NAME", "ROLE", "SERVE_URL", "BEARER_TOKEN",
    "/absolute/path/to/",
)

#: code -> (label template, command template). `{field}` slots in EITHER are
#: filled from `edge["facts"]`.
#:
#: Placeholders for values we CANNOT fill are bare UPPERCASE tokens, never
#: `<angle brackets>`: these strings are meant to be copied straight into a
#: shell, where `<role>` is input redirection (pastes and dies with "role: No
#: such file or directory") and `--remote=<role>` is a syntax error outright.
#: `tests/webui/test_actions.py` enforces this by running every rendered command
#: through `bash -n` and rejecting angle brackets.
_COMMANDS: dict[str, tuple[str, str]] = {
    "run_host_unmapped": (
        "Map this role on this machine",
        "mship run-host add {role} --pair-link 'PAIR_LINK'   # from `mship pair` on that machine",
    ),
    "run_host_stale_token": (
        "Re-map with a fresh token",
        "mship run-host add {role} --pair-link 'PAIR_LINK'   # a fresh link from `mship pair`",
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
        "run_hosts: [ROLE_NAME]   # add to mothership.yaml",
    ),
    "run_hosts_ambiguous_default": (
        "Name the role explicitly",
        "mship run --remote=ROLE   # or declare `run_host: ROLE` on the repo",
    ),
    "run_hosts_store_unreadable": (
        # The store file is the thing to fix first, so name it. The command then
        # has to carry a connection source: bare `run-host add <role>` is
        # REJECTED by the CLI ("provide a connection: either --url and --token
        # together, or --pair-link"), and a card that fails on paste is worse
        # than no card.
        "Fix or remove {store_path}, then re-map each role",
        "mship run-host add {role} --pair-link 'PAIR_LINK'   # repeat per declared role",
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
        "export MSHIP_GH_BROKER_URL=SERVE_URL MSHIP_SERVE_TOKEN=BEARER_TOKEN",
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
    label_template, command_template = entry
    facts = dict(edge.get("facts") or {})

    # The unreadable-store edge cannot report a `role` (reading the store is what
    # failed), but it DOES carry the roles declared in mothership.yaml — so fill a
    # real one instead of leaving a literal ROLE the operator has to guess at. The
    # comment in the template already says to repeat it per declared role.
    if "role" not in facts:
        declared = facts.get("declared") or []
        if declared:
            facts["role"] = declared[0]

    # Facts are CONFIG-DERIVED (role names come from mothership.yaml), and the
    # command is meant to be copied into a shell — so every substituted value is
    # shell-quoted. Jinja's HTML escaping protects the PAGE; it does nothing once
    # the text is pasted into a terminal, where a role like `x; touch /tmp/pwned`
    # would otherwise run (Greptile, PR #412). Labels are prose, not shell, so
    # they get the raw value.
    quoted = {k: shlex.quote(v) if isinstance(v, str) else v for k, v in facts.items()}

    def _fill(text: str, values: dict) -> str:
        """Substitute facts, leaving the placeholder visible when a fact is
        missing — a half-filled card still guides, and raising here would blank
        the whole page for one absent field."""
        try:
            return text.format(**values)
        except (KeyError, IndexError):
            return text

    command = _fill(command_template, quoted)
    return {
        "label": _fill(label_template, facts),
        "command": command,
        # True when the operator must substitute something before running.
        "needs_input": any(token in command for token in _PLACEHOLDERS),
    }
