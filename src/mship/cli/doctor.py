import os
from pathlib import Path

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Inspection")
    def doctor(
        no_network: bool = typer.Option(
            False, "--no-network",
            help="Skip connectivity network probes (faster; config-level checks only).",
        ),
        remote: str | None = typer.Option(
            None,
            "--remote",
            flag_value="",
            help="Inspect one selected run host; bare flag auto-resolves its role.",
        ),
        task: str | None = typer.Option(
            None, "--task", help="Active task whose branch is inspected remotely."
        ),
        repo: str | None = typer.Option(
            None, "--repo", help="Exactly one repository to inspect remotely."
        ),
    ):
        """Check workspace health and configuration."""
        container = get_container()
        output = Output()

        if remote is not None:
            if no_network:
                output.error("--no-network cannot be combined with --remote")
                raise typer.Exit(code=1)
            if not repo or "," in repo:
                output.error("--remote doctor requires exactly one --repo")
                raise typer.Exit(code=1)
            from mship.core.host_tools import remote_operation
            from mship.core.run_host import RunHostError, RunHostResolver, RunHostStore, resolve_run_host
            from mship.core.task_resolver import (
                AmbiguousTaskError,
                NoActiveTaskError,
                UnknownTaskError,
                resolve_task,
            )

            config = container.config()
            if repo not in config.repos:
                output.error(f"Unknown repo {repo!r}")
                raise typer.Exit(code=1)
            try:
                task_obj, _ = resolve_task(
                    container.state_manager().load(),
                    cli_task=task,
                    env_task=os.environ.get("MSHIP_TASK"),
                    cwd=Path.cwd(),
                )
            except (NoActiveTaskError, AmbiguousTaskError, UnknownTaskError):
                output.error("--remote doctor requires a resolvable active --task")
                raise typer.Exit(code=1)
            if repo not in task_obj.worktrees:
                output.error("--remote doctor repo is not part of the active task")
                raise typer.Exit(code=1)
            try:
                conn = resolve_run_host(
                    remote or None,
                    repo=config.repos[repo],
                    config=config,
                    store=RunHostStore(container.state_dir()),
                )
            except RunHostError as error:
                output.error(str(error))
                raise typer.Exit(code=1)
            report = remote_operation(
                action="diagnose",
                task_obj=task_obj,
                repo=repo,
                config=config,
                shell=container.shell(),
                host=conn,
                resolver=RunHostResolver(),
                output=output,
            )
            if report is None:
                output.error("selected run host did not return a valid host-tools report")
                raise typer.Exit(code=1)
            payload = report.safe_dict()
            if output.human_mode:
                output.print(f"[bold]Remote host tools:[/bold] {payload['category']}")
                output.print(f"  {payload['remediation']}")
            else:
                # Additive: retain local-doctor JSON keys when remote mode is used.
                output.json({
                    "checks": [],
                    "warnings": 0,
                    "errors": 0 if report.category == "healthy" else 1,
                    "config_path": str(Path(container.config_path()).resolve()),
                    "config_resolution_source": None,
                    "remote_host_tools": payload,
                })
            if report.category != "healthy":
                raise typer.Exit(code=1)
            return

        from mship.core.doctor import DoctorChecker
        from mship.core.config import ConfigLoader

        # issue 366 #5/#3: load with require_paths=False so a not-yet-present or
        # being-changed Taskfile.yml surfaces as a doctor `fail` check rather
        # than hard-failing ConfigLoader.load before doctor can run. The
        # container singleton keeps require_paths=True for spawn/finish/exec.
        config = ConfigLoader.load(container.config_path(), require_paths=False)
        shell = container.shell()

        # issue 366 #6: resolve which config is live + how it resolved, to report.
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
            probe_network=not no_network,
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
