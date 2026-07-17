import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Inspection")
    def doctor():
        """Check workspace health and configuration."""
        container = get_container()
        output = Output()

        from mship.core.doctor import DoctorChecker
        from mship.core.config import ConfigLoader

        # issue 366 #5/#3: load with require_paths=False so a not-yet-present or
        # being-changed Taskfile.yml surfaces as a doctor `fail` check rather
        # than hard-failing ConfigLoader.load before doctor can run. The
        # container singleton keeps require_paths=True for spawn/finish/exec.
        config = ConfigLoader.load(container.config_path(), require_paths=False)
        shell = container.shell()

        # issue 366 #6: resolve which config is live + how it resolved, to report.
        from pathlib import Path
        config_path = container.config_path()
        config_source = None
        try:
            res = ConfigLoader.discover_with_source(Path.cwd())
            if str(res.path.resolve()) == str(Path(config_path).resolve()):
                config_source = res.source
        except Exception:
            config_source = None

        checker = DoctorChecker(
            config,
            shell,
            state_dir=container.state_dir(),
            workspace_root=container.config_path().parent,
            config_path=config_path,
            config_source=config_source,
        )
        report = checker.run()

        if output.human_mode:
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
                "config_path": str(Path(config_path).resolve()),
                "config_resolution_source": config_source,
            })

        if not report.ok:
            raise typer.Exit(code=1)
