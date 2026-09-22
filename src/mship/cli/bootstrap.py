import os
from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output
from mship.cli.remote_flags import RemoteFlagCommand


def register(app: typer.Typer, get_container):
    @app.command(cls=RemoteFlagCommand, rich_help_panel="Setup")
    def bootstrap(
        repos: Optional[str] = typer.Option(
            None, "--repos", help="Comma-separated repo names (default: all)."
        ),
        token: Optional[str] = typer.Option(
            None, "--token", help="GitHub token for cloning private members "
            "(else GH_TOKEN / GITHUB_TOKEN).",
        ),
        relay_url: Optional[str] = typer.Option(
            None, "--relay-url",
            help="Relay egress base URL. With --run-token, route git through the "
                 "relay and clone with no GitHub token on the worker.",
        ),
        run_token: Optional[str] = typer.Option(
            None, "--run-token",
            help="Per-run relay token (paired with --relay-url).",
        ),
        host_tools: bool = typer.Option(
            False, "--host-tools", help="Provision only the reviewed mise host-tools declaration on one selected run host."
        ),
        remote: Optional[str] = typer.Option(
            None, "--remote", flag_value="", help="Selected run-host role; bare flag auto-resolves."
        ),
        task: Optional[str] = typer.Option(None, "--task", help="Active task to materialize on the selected host."),
        repo: Optional[str] = typer.Option(None, "--repo", help="Exactly one repository with host_tools declared."),
    ):
        """Clone missing workspace members so a fresh clone becomes a full workspace."""
        from mship.core.bootstrap import bootstrap as run_bootstrap

        container = get_container()
        output = Output()
        config_path = container.config_path()
        shell = container.shell()
        state_dir = container.state_dir()

        if host_tools:
            if repos or token or relay_url or run_token or remote is None or not task or not repo or "," in repo:
                output.error(
                    "--host-tools requires --remote --task and exactly one --repo; "
                    "it cannot be combined with --repos, clone tokens, or relay bootstrap flags"
                )
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
            if repo not in config.repos or config.repos[repo].host_tools is None:
                output.error("--host-tools requires a repository with a host_tools declaration")
                raise typer.Exit(code=1)
            try:
                task_obj, _ = resolve_task(
                    container.state_manager().load(),
                    cli_task=task,
                    env_task=os.environ.get("MSHIP_TASK"),
                    cwd=Path.cwd(),
                )
            except (NoActiveTaskError, AmbiguousTaskError, UnknownTaskError):
                output.error("--host-tools requires a resolvable active --task")
                raise typer.Exit(code=1)
            if repo not in task_obj.worktrees:
                output.error("--host-tools repo is not part of the active task")
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

            def events(event):
                data = getattr(event, "data", b"")
                if data:
                    output.progress(data.decode("utf-8", errors="replace").rstrip())

            report = remote_operation(
                action="bootstrap",
                task_obj=task_obj,
                repo=repo,
                config=config,
                shell=container.shell(),
                host=conn,
                resolver=RunHostResolver(),
                output=output,
                event_sink=events,
            )
            if report is None:
                output.error("selected run host did not return a valid host-tools report")
                raise typer.Exit(code=1)
            payload = report.safe_dict()
            if output.human_mode:
                outcome = "ready" if report.category == "healthy" else report.category
                output.print(f"[bold]Host tools:[/bold] {outcome}")
                output.print(f"  {payload['remediation']}")
            else:
                output.json({"host_tools": payload, "errors": 0 if report.category == "healthy" else 1})
            if report.category != "healthy":
                raise typer.Exit(code=1)
            return
        if remote is not None or task is not None or repo is not None:
            output.error("--remote, --task, and --repo require --host-tools")
            raise typer.Exit(code=1)
        from mship.core.relay.worker_config import relay_flags_error
        pair_error = relay_flags_error(relay_url, run_token)
        if pair_error:
            output.error(pair_error)
            raise typer.Exit(code=1)

        names = (
            [n.strip() for n in repos.split(",") if n.strip()] if repos else None
        )

        try:
            report = run_bootstrap(config_path, shell, state_dir=state_dir,
                                   repos=names, token=token,
                                   relay_url=relay_url, run_token=run_token)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        warnings: list[str] = []
        if report.doctor_ok is False:
            warnings.append("doctor reported issues — run `mship doctor`")
        elif report.doctor_ok is None and not report.has_errors:
            warnings.append("doctor was not run")

        if output.human_mode:
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
