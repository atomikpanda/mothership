"""Worker-side glue for attach-at-relay boots (spec relay-aware-worker-boot).

Emits the `git config --global` commands that route a DISPOSABLE cloud
worker's git + GitHub-API traffic through the relay egress-proxy, and
validates the --relay-url/--run-token flag pair. The path prefixes and the
run-token header name come from core/relay/contract.py — the SAME constants the
egress-proxy parses — so this config can never drift from the server.
"""
from __future__ import annotations

import shlex

from mship.core.relay.contract import PREFIX_HOST, RUN_TOKEN_HEADER


def relay_git_config_commands(relay_url: str, run_token: str) -> list[str]:
    """The `git config --global` shell commands that point a worker's git at
    the relay:

      - one `url.<relay><prefix>.insteadOf https://<host>/` rewrite per egress
        prefix (so `git clone https://github.com/o/r` -> `<relay>/gh/o/r`, and
        every REST call to api.github.com -> `<relay>/api/...`), and
      - `http.<relay>/.extraHeader <RUN_TOKEN_HEADER>: <run_token>` so every
        relay-bound request carries the low-value per-run token.

    Ready to hand to ShellRunner.run() (shell=True); values are shlex-quoted.
    """
    base = relay_url.rstrip("/")
    cmds: list[str] = []
    for prefix, host in PREFIX_HOST.items():
        key = f"url.{base}{prefix}.insteadOf"
        val = f"https://{host}/"
        cmds.append(f"git config --global {shlex.quote(key)} {shlex.quote(val)}")
    header_key = f"http.{base}/.extraHeader"
    header_val = f"{RUN_TOKEN_HEADER}: {run_token}"
    cmds.append(
        f"git config --global {shlex.quote(header_key)} {shlex.quote(header_val)}"
    )
    return cmds


def relay_flags_error(relay_url: str | None, run_token: str | None) -> str | None:
    """Validate the --relay-url/--run-token pair. They are a unit — relay-attach
    mode needs both. Returns a clear error string when exactly one is given,
    else None (both => relay mode; neither => the command's non-relay behavior)."""
    if bool(relay_url) == bool(run_token):
        return None
    missing = "--run-token" if relay_url else "--relay-url"
    return f"relay mode requires both --relay-url and --run-token; {missing} is missing"
