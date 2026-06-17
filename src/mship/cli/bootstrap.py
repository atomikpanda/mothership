from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def bootstrap(
        repos: Optional[str] = typer.Option(
            None, "--repos", help="Comma-separated repo names (default: all)."
        ),
    ):
        """Clone missing workspace members so a fresh clone becomes a full workspace."""
        from mship.core.bootstrap import bootstrap as run_bootstrap

        container = get_container()
        output = Output()
        config_path = container.config_path()
        shell = container.shell()
        state_dir = container.state_dir()

        names = (
            [n.strip() for n in repos.split(",") if n.strip()] if repos else None
        )

        try:
            report = run_bootstrap(config_path, shell, state_dir=state_dir, repos=names)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        warnings: list[str] = []
        if report.doctor_ok is False:
            warnings.append("doctor reported issues — run `mship doctor`")
        elif report.doctor_ok is None and not report.has_errors:
            warnings.append("doctor was not run")

        if output.is_tty:
            for m in report.members:
                if m.status == "cloned":
                    output.print(f"  [green]{m.name}[/green]: {m.message}")
                elif m.status == "present":
                    output.print(f"  {m.name}: {m.message}")
                else:
                    output.print(f"  [red]{m.name}[/red]: {m.message}")
            for w in warnings:
                output.warning(w)
        else:
            output.json({
                "members": [
                    {"name": m.name, "status": m.status, "message": m.message}
                    for m in report.members
                ],
                "doctor_ok": report.doctor_ok,
                "warnings": warnings,
                "errors": sum(1 for m in report.members if m.status == "error"),
            })

        raise typer.Exit(code=1 if report.has_errors else 0)
