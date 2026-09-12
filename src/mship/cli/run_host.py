"""Private named run-host registry management."""
from __future__ import annotations

from typing import Literal

import typer

from mship.cli.output import Output


def register(parent: typer.Typer, get_container):
    run_host_app = typer.Typer(
        name="run-host", help="Manage private named run-host connections.", no_args_is_help=True,
    )

    from mship.core.run_host.store import RunHostError

    def registry_failure(out: Output) -> None:
        out.error("could not read private run-host registry; fix the private file and retry")
        raise typer.Exit(1)

    def store():
        from mship.core.run_host.store import RunHostStore
        return RunHostStore(get_container().state_dir())

    def connection(url: str | None, token: str | None, pair_link: str | None):
        from mship.core.relay.pairing import parse_pair_link
        out = Output()
        if pair_link is not None and (url is not None or token is not None):
            out.error("pass either --url/--token or --pair-link, not both")
            raise typer.Exit(2)
        if pair_link is not None:
            try:
                parsed = parse_pair_link(pair_link)
            except ValueError as exc:
                out.error(f"invalid --pair-link: {exc}")
                raise typer.Exit(2)
            return parsed["url"], parsed["token"]
        if url is not None and token is not None:
            return url, token
        out.error("provide a connection: either --url and --token together, or --pair-link")
        raise typer.Exit(2)

    @run_host_app.command("add")
    def add(
        name: str = typer.Argument(..., help="Private host name."),
        role: list[str] = typer.Option(None, "--role", help="Advertised role (repeatable; defaults to NAME)."),
        scope: Literal["user", "project"] = typer.Option("project", "--scope", help="Registration scope."),
        url: str | None = typer.Option(None, "--url", help="Run-host base URL. Requires --token."),
        token: str | None = typer.Option(None, "--token", help="Bearer token. Requires --url."),
        pair_link: str | None = typer.Option(None, "--pair-link", help="Ground Control pair link."),
        tag: list[str] = typer.Option(None, "--tag", help="Host tag (repeatable)."),
        preference: int = typer.Option(0, "--preference", help="Host preference among equal targets."),
    ):
        """Add or replace one complete named host entry in its chosen scope."""
        from mship.core.run_host.config import HostRegistration, RunHostConnection
        resolved_url, resolved_token = connection(url, token, pair_link)
        roles = tuple(role or [name])
        try:
            store().set_host(HostRegistration(name, roles, tuple(tag or ()), preference,
                                              RunHostConnection(resolved_url, resolved_token), scope), scope=scope)
        except (RunHostError, OSError, UnicodeError):
            registry_failure(Output())
        Output().success(f"registered run-host {name!r} in {scope} scope")

    @run_host_app.command("list")
    def list_cmd():
        """List safe effective host summaries; tokens are never displayed."""
        out = Output()
        try:
            entries = store().safe_hosts()
        except (RunHostError, OSError, UnicodeError):
            registry_failure(out)
        if not entries:
            out.print("no run-hosts configured")
            return
        out.table(title="Run hosts", columns=["Name", "Roles", "URL", "Scope"],
                  rows=[[name, ", ".join(host["roles"]), host["url"], host["scope"]]
                        for name, host in sorted(entries.items())])

    @run_host_app.command("remove")
    def remove(
        name: str = typer.Argument(..., help="Host name to remove."),
        scope: Literal["user", "project"] = typer.Option("project", "--scope", help="Registration scope."),
    ):
        """Remove a scoped host entry (a missing entry is a no-op)."""
        try:
            store().remove_host(name, scope=scope)
        except (RunHostError, OSError, UnicodeError):
            registry_failure(Output())
        Output().success(f"removed run-host {name!r} from {scope} scope")

    @run_host_app.command("allow-role")
    def allow_role(
        role: str = typer.Argument(..., help="Role whose project eligibility policy is changed."),
        host: list[str] = typer.Option(None, "--host", help="Allowed host name (repeatable)."),
        all_hosts: bool = typer.Option(False, "--all", help="Opt into all hosts advertising this role."),
    ):
        """Set project-only role eligibility; --all removes the restriction."""
        out = Output()
        if all_hosts == bool(host):
            out.error("pass exactly one of --host or --all")
            raise typer.Exit(2)
        try:
            store().set_role_hosts(role, None if all_hosts else host)
        except (RunHostError, OSError, UnicodeError):
            registry_failure(out)
        out.success(f"updated allowed hosts for role {role!r}")

    @run_host_app.command("migrate")
    def migrate(
        scope: Literal["user", "project"] = typer.Option("project", "--scope", help="Legacy registry scope."),
        apply: bool = typer.Option(False, "--apply", help="Write backup and convert the legacy registry."),
    ):
        """Preview, then explicitly convert, a legacy private registry."""
        out = Output()
        allowed = get_container().config().run_hosts
        try:
            report = store().migrate(scope=scope, allowed_roles=allowed, apply=apply)
        except (RunHostError, OSError, UnicodeError):
            registry_failure(out)
        if not report.changed:
            out.print(f"no legacy {scope} run-host registry needs migration")
        elif not apply:
            out.print(f"would migrate {scope} registry hosts: {', '.join(report.hosts) or '(none)'}; rerun with --apply")
        else:
            out.success(f"migrated {scope} registry; private backup: {report.backup_path}")

    parent.add_typer(run_host_app, rich_help_panel="Runtime")
