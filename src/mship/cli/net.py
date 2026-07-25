"""`mship net` — report this machine's connectivity topology.

A thin caller over `mship.core.topology.probe_topology`: the same structure the
`doctor` connectivity group and `GET /net/topology` report, rendered for a
terminal. Exits 0 even when every edge is broken — this command has to work
precisely when connectivity does not.
"""
from __future__ import annotations

import typer

from mship.cli.output import Output

_ICON = {"ok": "[green]OK[/green]", "warn": "[yellow]WARN[/yellow]",
         "fail": "[red]FAIL[/red]", "absent": "[dim]--[/dim]"}


def register(parent: typer.Typer, get_container):
    net_app = typer.Typer(
        name="net",
        help="Inspect connectivity: serve, relay, run hosts, GitHub auth, egress.",
        no_args_is_help=True,
    )

    @net_app.command("status")
    def status(
        no_network: bool = typer.Option(
            False, "--no-network",
            help="Skip network probes; report configured state only.",
        ),
    ):
        """Show this machine's connectivity topology and per-edge health."""
        from mship.core import topology as topo
        from mship.core.config import ConfigLoader

        out = Output()
        container = get_container()
        # require_paths=False so a half-configured workspace still reports its
        # connectivity — the same reason `doctor` loads this way.
        config = ConfigLoader.load(container.config_path(), require_paths=False)

        result = topo.probe_topology(
            config=config,
            state_dir=container.state_dir(),
            workspace_root=container.config_path().parent,
            skip_network=no_network,
        )

        if out.json_mode:
            out.json(topo.topology_payload(result))
            return

        out.print(f"[bold]Workspace:[/bold] {result.workspace}   "
                  f"[dim]probed {result.probed_at}[/dim]\n")
        out.table(
            title="Connectivity",
            columns=["", "Edge", "State", "Detail"],
            rows=[
                [_ICON.get(e.status, e.status), e.name, e.code, e.detail]
                for e in result.edges
            ],
        )
        fixes = [e for e in result.edges if e.fix and e.status in ("warn", "fail")]
        if fixes:
            out.print("\n[bold]Next steps:[/bold]")
            for e in fixes:
                out.print(f"  [yellow]{e.name}[/yellow]: {e.fix}")

    # "Inspection" alongside `doctor`/`status`: this reports, it never mutates.
    parent.add_typer(net_app, rich_help_panel="Inspection")
