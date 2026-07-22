"""`mship gh` sub-app — GitHub auth helpers.

`mship gh preflight` is a fail-fast check meant to run FIRST in an unattended
(overnight) session: unlike `gh_auth.resolve_token` (which silently degrades
to "no token" on any broker error so bootstrap/finish can still proceed),
this is STRICT — a broker error or missing auth aborts loudly with an
actionable, repo-naming message, before any AI/token spend on code that
could then not be pushed.
"""
from __future__ import annotations

from typing import Optional

import typer

from mship.cli.output import Output


def register(parent: typer.Typer, get_container):
    gh_app = typer.Typer(
        name="gh",
        help="GitHub auth helpers (fail-fast preflight checks for unattended runs).",
        no_args_is_help=True,
    )

    @gh_app.command("preflight")
    def preflight(
        repos: Optional[str] = typer.Option(
            None, "--repos",
            help="Comma-separated repo names to check (default: every non-git_root "
                 "repo in the workspace — the same set bootstrap/finish use).",
        ),
        token: Optional[str] = typer.Option(
            None, "--token",
            help="Explicit GitHub token override (else GH_TOKEN / GITHUB_TOKEN).",
        ),
        relay_url: Optional[str] = typer.Option(
            None, "--relay-url",
            help="Relay egress base URL. With --run-token, verify auth through "
                 "the relay (attach-at-relay) instead of a GH token / broker.",
        ),
        run_token: Optional[str] = typer.Option(
            None, "--run-token",
            help="Per-run relay token (paired with --relay-url).",
        ),
    ):
        """Fail-fast check that GitHub auth actually covers the workspace repos,
        BEFORE an unattended run spends AI tokens on code it can't then push.

        STRICT by design — the opposite of `resolve_token`'s resilient
        swallow: a broker error or missing auth exits non-zero with a clear,
        actionable message instead of quietly degrading to no auth."""
        from mship.core.gh_auth import broker_config_from_env
        from mship.core.gh_preflight import (
            repo_owner_names_from_config,
            repo_set_from_config,
            run_preflight,
        )

        output = Output()
        container = get_container()
        config_path = container.config_path()

        from mship.core.relay.worker_config import relay_flags_error
        pair_error = relay_flags_error(relay_url, run_token)
        if pair_error:
            output.error(pair_error)
            raise typer.Exit(code=1)

        explicit_repos = (
            [n.strip() for n in repos.split(",") if n.strip()] if repos else None
        )
        resolved_repos = repo_set_from_config(config_path, explicit_repos)
        # Only needed on the override-token path (see run_preflight), but
        # resolving it is pure config parsing (no network) so it's cheap to
        # compute unconditionally here.
        repo_owner_names = repo_owner_names_from_config(config_path, resolved_repos)

        broker_url, broker_bearer = broker_config_from_env()
        result = run_preflight(
            explicit_token=token,
            broker_url=broker_url,
            broker_bearer=broker_bearer,
            repos=resolved_repos,
            repo_owner_names=repo_owner_names,
            relay_url=relay_url,
            run_token=run_token,
        )

        if result.ok:
            output.success(result.message)
            raise typer.Exit(code=0)
        output.error(result.message)
        raise typer.Exit(code=1)

    parent.add_typer(gh_app, rich_help_panel="Setup")
