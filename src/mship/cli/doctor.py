import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def doctor():
        """Check workspace health and configuration."""
        container = get_container()
        output = Output()

        from mship.core.doctor import DoctorChecker

        config = container.config()
        shell = container.shell()
        checker = DoctorChecker(
            config,
            shell,
            state_dir=container.state_dir(),
            workspace_root=container.config_path().parent,
        )
        report = checker.run()

        if output.is_tty:
            output.print(f"[bold]Workspace:[/bold] {config.workspace}\n")

            current_repo = None
            for check in report.checks:
                # Group by repo
                parts = check.name.split("/", 1)
                repo = parts[0] if len(parts) > 1 else None

                if repo and repo != current_repo:
                    current_repo = repo
                    output.print(f"[bold]{repo}:[/bold]")

                if check.status == "pass":
                    icon = "[green]✓[/green]"
                elif check.status == "warn":
                    icon = "[yellow]⚠[/yellow]"
                else:
                    icon = "[red]✗[/red]"

                output.print(f"  {icon} {check.message}")

            output.print("")
            if report.errors > 0:
                output.error(f"{report.errors} error(s), {report.warnings} warning(s)")
            elif report.warnings > 0:
                output.success(f"All checks passed ({report.warnings} warning(s))")
            else:
                output.success("All checks passed")
        else:
            output.json({
                "checks": [{"name": c.name, "status": c.status, "message": c.message} for c in report.checks],
                "warnings": report.warnings,
                "errors": report.errors,
            })

        if not report.ok:
            raise typer.Exit(code=1)
