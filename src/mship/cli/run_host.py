"""Private named direct and relay run-host registry management."""
from __future__ import annotations

from typing import Literal

import typer

from mship.cli.output import Output


def register(parent: typer.Typer, get_container):
    run_host_app = typer.Typer(name="run-host", help="Manage private named direct or relay run hosts.", no_args_is_help=True)

    from mship.core.run_host.store import RunHostError

    def registry_failure(out: Output, error: Exception) -> None:
        out.error(str(error) if isinstance(error, RunHostError) else "could not update private run-host state; fix the private file and retry")
        raise typer.Exit(1)

    def store():
        from mship.core.run_host.store import RunHostStore
        return RunHostStore(get_container().state_dir())

    def resolver():
        from mship.core.run_host.resolver import RunHostResolver
        return RunHostResolver()

    def direct_connection(url: str | None, token: str | None, pair_link: str | None):
        from mship.core.relay.pairing import parse_pair_link
        if pair_link is not None and (url is not None or token is not None):
            raise RunHostError("pass either --url/--token or --pair-link, not both")
        if pair_link is not None:
            try:
                parsed = parse_pair_link(pair_link)
            except ValueError as exc:
                raise RunHostError(f"invalid --pair-link: {exc}") from None
            return parsed["url"], parsed["token"]
        if url is not None and token is not None:
            return url, token
        raise RunHostError("provide a direct connection with --url and --token, or --pair-link")

    @run_host_app.command("pair-relay")
    def pair_relay():
        """Store one already-issued relay account link without printing its credential."""
        from mship.core.relay.pairing import parse_relay_account_link
        from mship.core.run_host.pairing_store import RelayPairingStore
        try:
            link = typer.prompt("Relay account link", hide_input=True)
            parsed = parse_relay_account_link(link)
            RelayPairingStore().put(parsed["relay"], parsed["token"])
        except (RunHostError, ValueError, OSError, UnicodeError) as exc:
            registry_failure(Output(), exc)
        Output().success("stored private relay pairing; add a selected relay run host next")

    @run_host_app.command("add")
    def add(
        name: str = typer.Argument(..., help="Private host name."),
        role: list[str] = typer.Option(None, "--role", help="Advertised role (repeatable; defaults to NAME)."),
        scope: Literal["user", "project"] = typer.Option("project", "--scope", help="Registration scope."),
        url: str | None = typer.Option(None, "--url", help="Direct run-host base URL. Requires --token."),
        token: str | None = typer.Option(None, "--token", help="Direct bearer token. Requires --url."),
        pair_link: str | None = typer.Option(None, "--pair-link", help="Direct Ground Control pair link."),
        relay: str | None = typer.Option(None, "--relay", help="Relay domain for an already paired enrolled host."),
        host_id: str | None = typer.Option(None, "--host-id", help="Approved relay directory host identity."),
        workspace_id: str | None = typer.Option(None, "--workspace-id", help="Workspace to bind below the selected host."),
        tag: list[str] = typer.Option(None, "--tag", help="Host tag (repeatable)."),
        preference: int = typer.Option(0, "--preference", help="Host preference among equal targets."),
    ):
        """Add a direct mapping or selected relay identity; never persist a relay credential."""
        from mship.core.run_host.config import RunHostConnection, HostRegistration
        relay_values = (relay, host_id, workspace_id)
        is_relay = any(value is not None for value in relay_values)
        is_direct = any(value is not None for value in (url, token, pair_link))
        if is_relay and is_direct:
            registry_failure(Output(), RunHostError("pass either direct --url/--token/--pair-link or relay --relay/--host-id/--workspace-id"))
        try:
            if is_relay:
                if not all(relay_values):
                    raise RunHostError("relay registration requires --relay, --host-id, and --workspace-id")
                connection = resolver().select_relay_identity(relay=relay, host_id=host_id, workspace_id=workspace_id)
            else:
                resolved_url, resolved_token = direct_connection(url, token, pair_link)
                connection = RunHostConnection(resolved_url, resolved_token)
            roles = tuple(role or [name])
            store().set_host(HostRegistration(name, roles, tuple(tag or ()), preference, connection, scope), scope=scope)
        except (RunHostError, OSError, UnicodeError, ValueError) as exc:
            registry_failure(Output(), exc)
        Output().success(f"registered {'relay' if is_relay else 'direct'} run-host {name!r} in {scope} scope")

    @run_host_app.command("list")
    def list_cmd():
        """List safe effective host summaries; credentials and mutable relay URLs are never displayed."""
        out = Output()
        try:
            entries = store().safe_hosts()
        except (RunHostError, OSError, UnicodeError) as exc:
            registry_failure(out, exc)
        if not entries:
            out.print("no run-hosts configured")
            return
        rows = []
        for name, host in sorted(entries.items()):
            destination = host["url"] if host["mode"] == "direct" else f"{host['relay']} / {host['host_id']} / {host['workspace_id']}"
            rows.append([name, ", ".join(host["roles"]), host["mode"], destination, host["scope"]])
        out.table(title="Run hosts", columns=["Name", "Roles", "Mode", "Destination", "Scope"], rows=rows)

    @run_host_app.command("remove")
    def remove(name: str = typer.Argument(..., help="Host name to remove."), scope: Literal["user", "project"] = typer.Option("project", "--scope", help="Registration scope.")):
        """Remove a scoped host entry (a missing entry is a no-op)."""
        try:
            store().remove_host(name, scope=scope)
        except (RunHostError, OSError, UnicodeError) as exc:
            registry_failure(Output(), exc)
        Output().success(f"removed run-host {name!r} from {scope} scope")

    @run_host_app.command("allow-role")
    def allow_role(role: str = typer.Argument(..., help="Role whose project eligibility policy is changed."), host: list[str] = typer.Option(None, "--host", help="Allowed host name (repeatable)."), all_hosts: bool = typer.Option(False, "--all", help="Opt into all hosts advertising this role.")):
        """Set project-only role eligibility; --all removes the restriction."""
        out = Output()
        if all_hosts == bool(host):
            out.error("pass exactly one of --host or --all")
            raise typer.Exit(2)
        try:
            store().set_role_hosts(role, None if all_hosts else host)
        except (RunHostError, OSError, UnicodeError) as exc:
            registry_failure(out, exc)
        out.success(f"updated allowed hosts for role {role!r}")

    @run_host_app.command("migrate")
    def migrate(
        name: str | None = typer.Argument(None, help="Named direct host to migrate to relay mode."),
        scope: Literal["user", "project"] = typer.Option("project", "--scope", help="Registry scope."),
        relay: str | None = typer.Option(None, "--relay", help="Relay domain for named relay migration."),
        host_id: str | None = typer.Option(None, "--host-id", help="Approved relay directory host identity."),
        workspace_id: str | None = typer.Option(None, "--workspace-id", help="Workspace to bind below the selected host."),
        apply: bool = typer.Option(False, "--apply", help="Write a private backup and make the requested migration."),
    ):
        """Preview then apply generic direct-format or explicit named relay migration."""
        out = Output()
        relay_values = (relay, host_id, workspace_id)
        try:
            if name is None:
                if any(value is not None for value in relay_values):
                    raise RunHostError("relay migration requires a named direct host")
                report = store().migrate(scope=scope, allowed_roles=get_container().config().run_hosts, apply=apply)
                if not report.changed:
                    out.print(f"no legacy {scope} run-host registry needs migration")
                elif not apply:
                    out.print(f"would migrate {scope} registry direct hosts: {', '.join(report.hosts) or '(none)'}; rerun with --apply")
                else:
                    out.success(f"migrated {scope} direct registry; private backup: {report.backup_path}")
                return
            if not all(relay_values):
                raise RunHostError("named relay migration requires --relay, --host-id, and --workspace-id")
            identity = resolver().select_relay_identity(relay=relay, host_id=host_id, workspace_id=workspace_id)
            report = store().replace_direct_with_relay(name, scope=scope, identity=identity, apply=apply)
            safe_identity = f"{identity.relay} / {identity.host_id} / {identity.workspace_id}"
            if apply:
                out.success(f"migrated {name!r} to relay identity {safe_identity}; private backup: {report.backup_path}")
            else:
                out.print(f"would migrate {name!r} to relay identity {safe_identity}; rerun with --apply")
        except (RunHostError, OSError, UnicodeError, ValueError) as exc:
            registry_failure(out, exc)

    parent.add_typer(run_host_app, rich_help_panel="Runtime")
