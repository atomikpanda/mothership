from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def audit(
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names"),
        json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    ):
        """Report git-state drift across workspace repos."""
        from mship.core.repo_state import audit_repos

        container = get_container()
        output = Output()
        config = container.config()
        shell = container.shell()

        names: list[str] | None = None
        if repos:
            names = [n.strip() for n in repos.split(",") if n.strip()]

        try:
            report = audit_repos(config, shell, names=names)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if json_output:
            import json as _json
            print(_json.dumps(report.to_json(workspace=config.workspace), indent=2))
            raise typer.Exit(code=1 if report.has_errors else 0)

        output.print(f"[bold]workspace:[/bold] {config.workspace}")
        output.print("")
        err_count = 0
        info_count = 0
        for r in report.repos:
            branch_suffix = f" ({r.current_branch})" if r.current_branch else ""
            output.print(f"[bold]{r.name}[/bold]{branch_suffix}:")
            if not r.issues:
                output.print("  [green]✓[/green] clean")
            else:
                for i in r.issues:
                    if i.severity == "error":
                        err_count += 1
                        output.print(f"  [red]✗[/red] {i.code}: {i.message}")
                    else:
                        info_count += 1
                        output.print(f"  [blue]ⓘ[/blue] {i.code}: {i.message}")
            output.print("")
        output.print(f"{err_count} error(s), {info_count} info across {len(report.repos)} repos")
        raise typer.Exit(code=1 if report.has_errors else 0)
