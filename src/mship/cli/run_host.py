"""`mship run-host` — manage the per-machine run-host connection store.

`mothership.yaml` declares only logical role *names* (`run_hosts: [...]`,
`RepoConfig.run_host`) — see `mship.core.config`. This command group manages
the concrete, gitignored `{role: {url, token}}` mapping that lives at
`<state_dir>/run-hosts.yaml` (`RunHostStore`, in `mship.core.run_host.store`),
which `resolve_run_host` reads at invocation time (`--remote[=role]`).

`add` accepts a connection either directly (`--url` + `--token`) or via a
pasted Ground Control pair link (`--pair-link`, the same
`groundcontrol://add?...` shape `mship pair` prints) — exactly one source.
"""
from __future__ import annotations

import typer

from mship.cli.output import Output


def register(parent: typer.Typer, get_container):
    run_host_app = typer.Typer(
        name="run-host",
        help="Manage per-machine run-host connections (role -> {url, token}).",
        no_args_is_help=True,
    )

    @run_host_app.command("add")
    def add(
        role: str = typer.Argument(..., help="Logical run-host role name (declared in mothership.yaml's run_hosts:)."),
        url: str = typer.Option(None, "--url", help="Run-host base URL. Requires --token; mutually exclusive with --pair-link."),
        token: str = typer.Option(None, "--token", help="Run-host bearer token. Requires --url; mutually exclusive with --pair-link."),
        pair_link: str = typer.Option(None, "--pair-link", help="A pasted groundcontrol://add?... pair link to parse url+token from, instead of --url/--token."),
    ):
        """Map a run-host role to a connection ({url, token})."""
        from mship.core.relay.pairing import parse_pair_link
        from mship.core.run_host.config import RunHostConnection
        from mship.core.run_host.store import RunHostStore

        out = Output()
        has_pair_link = pair_link is not None
        has_url_or_token = url is not None or token is not None

        if has_pair_link and has_url_or_token:
            out.error("pass either --url/--token or --pair-link, not both")
            raise typer.Exit(2)

        if has_pair_link:
            try:
                parsed = parse_pair_link(pair_link)
            except ValueError as exc:
                out.error(f"invalid --pair-link: {exc}")
                raise typer.Exit(2)
            resolved_url, resolved_token = parsed["url"], parsed["token"]
        elif url is not None and token is not None:
            resolved_url, resolved_token = url, token
        else:
            out.error("provide a connection: either --url and --token together, or --pair-link")
            raise typer.Exit(2)

        state_dir = get_container().state_dir()
        RunHostStore(state_dir).set(role, RunHostConnection(url=resolved_url, token=resolved_token))
        out.success(f"run-host {role!r} -> {resolved_url}")

    @run_host_app.command("list")
    def list_cmd():
        """List configured run-host roles and URLs (tokens are never shown)."""
        from mship.core.run_host.store import RunHostStore

        out = Output()
        state_dir = get_container().state_dir()
        entries = RunHostStore(state_dir).redacted_list()
        if not entries:
            out.print("no run-hosts configured")
            return
        out.table(
            title="Run hosts",
            columns=["Role", "URL"],
            rows=[[role, url] for role, url in entries],
        )

    @run_host_app.command("remove")
    def remove(
        role: str = typer.Argument(..., help="Run-host role name to remove."),
    ):
        """Remove a role's mapped connection (a no-op if it isn't mapped)."""
        from mship.core.run_host.store import RunHostStore

        out = Output()
        state_dir = get_container().state_dir()
        RunHostStore(state_dir).remove(role)
        out.success(f"removed run-host {role!r}")

    parent.add_typer(run_host_app)
