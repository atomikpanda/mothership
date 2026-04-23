"""`mship bind` sub-app — bind_files maintenance. See #71."""
from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    bind_app = typer.Typer(help="Manage bind_files across task worktrees.")

    @bind_app.command()
    def refresh(
        repos: Optional[str] = typer.Option(
            None, "--repos",
            help="Comma-separated repo names. Default: all affected_repos.",
        ),
        overwrite: bool = typer.Option(
            False, "--overwrite",
            help="Replace worktree copies even when they differ from source. "
                 "Without this flag, modified copies are preserved and the "
                 "command exits non-zero.",
        ),
        task: Optional[str] = typer.Option(
            None, "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env.",
        ),
    ):
        """Re-sync bind_files from source repos into the task's worktrees."""
        container = get_container()
        output = Output()
        state = container.state_manager().load()
        resolved = resolve_for_command("bind refresh", state, task, output)
        t = resolved.task
        config = container.config()
        wt_mgr = container.worktree_manager()

        repo_list = repos.split(",") if repos else list(t.affected_repos)
        unknown = [r for r in repo_list if r not in t.affected_repos]
        if unknown:
            output.error(
                f"--repos references repos not in task.affected_repos: {unknown}. "
                f"Task repos: {sorted(t.affected_repos)}"
            )
            raise typer.Exit(code=1)

        per_repo: list[dict] = []
        any_skipped = False

        for repo_name in repo_list:
            repo_cfg = config.repos[repo_name]
            wt = t.worktrees.get(repo_name)
            if wt is None:
                output.warning(f"{repo_name}: no worktree registered (skipping)")
                continue
            wt_path = Path(wt)
            if not wt_path.is_dir():
                output.warning(f"{repo_name}: worktree missing at {wt_path} (skipping)")
                continue

            result = wt_mgr.refresh_bind_files(
                repo_name, repo_cfg, wt_path, overwrite=overwrite,
            )
            per_repo.append({"repo": repo_name, **result})
            if result["skipped"]:
                any_skipped = True

        if output.is_tty:
            for row in per_repo:
                output.print(f"[bold]{row['repo']}[/bold]")
                for rel in row["copied"]:
                    output.print(f"  [green]copied[/green]    {rel}")
                for rel in row["updated"]:
                    output.print(f"  [yellow]updated[/yellow]   {rel}")
                for rel in row["unchanged"]:
                    output.print(f"  unchanged {rel}")
                for rel in row["skipped"]:
                    output.print(
                        f"  [red]skipped[/red]   {rel} (worktree-modified; pass --overwrite to replace)"
                    )
                for w in row["warnings"]:
                    output.warning(w)
            if any_skipped and not overwrite:
                output.print("")
                output.print(
                    "[yellow]Some files were skipped because they differ from source. "
                    "Re-run with --overwrite to replace them.[/yellow]"
                )
        else:
            output.json({
                "task": t.slug,
                "overwrite": overwrite,
                "repos": per_repo,
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })

        if any_skipped and not overwrite:
            raise typer.Exit(code=1)

    app.add_typer(bind_app, name="bind")
