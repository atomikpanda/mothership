from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def sync(
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names"),
    ):
        """Fast-forward repos that audit cleanly and are behind origin."""
        from mship.core.repo_state import audit_repos
        from mship.core.repo_sync import sync_repos

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

        out = sync_repos(report, config, shell)
        for r in out.results:
            if r.status == "up_to_date":
                output.print(f"  {r.name}: up to date")
            elif r.status == "fast_forwarded":
                output.print(f"  [green]{r.name}[/green]: fast-forwarded ({r.message})")
            else:
                output.print(f"  [yellow]{r.name}[/yellow]: skipped ({r.message})")

        raise typer.Exit(code=1 if out.has_errors else 0)
