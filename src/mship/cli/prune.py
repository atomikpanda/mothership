import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def prune(
        force: bool = typer.Option(False, "--force", help="Actually remove orphaned worktrees"),
    ):
        """Find and clean up orphaned worktrees."""
        container = get_container()
        output = Output()
        prune_mgr = container.prune_manager()

        orphans = prune_mgr.scan()

        if not orphans:
            if output.is_tty:
                output.success("No orphaned worktrees found")
            else:
                output.json({"orphans": [], "pruned": False})
            return

        if not force:
            if output.is_tty:
                output.warning(f"Found {len(orphans)} orphaned worktree(s):")
                for o in orphans:
                    output.print(f"  {o.repo}: {o.path} ({o.reason})")
                output.print("\nRun `mship prune --force` to clean up.")
            else:
                output.json({
                    "orphans": [
                        {"repo": o.repo, "path": str(o.path), "reason": o.reason}
                        for o in orphans
                    ],
                    "pruned": False,
                })
            return

        count = prune_mgr.prune(orphans)
        if output.is_tty:
            output.success(f"Pruned {count} item(s)")
        else:
            output.json({"pruned": True, "count": count})
