import typer

from mship.cli.output import Output
from mship.core.retained_worktrees import RetainedWorktreeStoreError


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Maintenance")
    def prune(
        force: bool = typer.Option(
            False,
            "--force",
            help="Execute the displayed cleanup plan",
        ),
        writers_stopped: bool = typer.Option(
            False,
            "--writers-stopped",
            help="Acknowledge that all filesystem and Git writers are stopped",
        ),
    ):
        """Preview or explicitly clean orphaned and retained worktrees."""
        container = get_container()
        output = Output()
        prune_mgr = container.prune_manager()

        try:
            orphans = prune_mgr.scan()
        except RetainedWorktreeStoreError as exc:
            output.error(
                f"Cannot scan retained worktree records: {exc}. No cleanup was performed."
            )
            raise typer.Exit(code=1)

        def orphan_payload(orphan):
            payload = {
                "repo": orphan.repo,
                "path": str(orphan.path),
                "reason": orphan.reason,
            }
            if orphan.retained_record is not None:
                payload["retained_record"] = str(orphan.retained_record)
            return payload

        def preview() -> None:
            if output.human_mode:
                output.warning(f"Found {len(orphans)} cleanup candidate(s):")
                for orphan in orphans:
                    record = (
                        f"; retained record: {orphan.retained_record}"
                        if orphan.retained_record is not None
                        else ""
                    )
                    output.print(
                        f"  {orphan.repo}: {orphan.path} ({orphan.reason}{record})"
                    )
                output.print(
                    "\nRun `mship prune --force --writers-stopped` to clean up."
                )
            else:
                output.json(
                    {
                        "orphans": [orphan_payload(orphan) for orphan in orphans],
                        "pruned": False,
                    }
                )

        if not orphans:
            if output.human_mode:
                output.success("No orphaned or retained worktrees found")
            else:
                output.json({"orphans": [], "pruned": False})
            return

        if not force:
            preview()
            return

        if not writers_stopped:
            preview()
            output.error(
                "Refusing to prune without --writers-stopped; no cleanup was performed."
            )
            raise typer.Exit(code=2)

        from mship.core.workitem_lifecycle import TaskMetadataRetentionConflictError

        try:
            result = prune_mgr.prune(orphans, writers_stopped=True)
        except (TaskMetadataRetentionConflictError, RetainedWorktreeStoreError) as exc:
            output.error(str(exc))
            raise typer.Exit(code=1)

        if output.human_mode:
            for removed in result.removed:
                output.success(
                    f"Removed {removed.repo}: {removed.path} "
                    f"(recovery ref: {removed.recovery_ref})"
                )
            if result.stale_mappings_removed:
                output.success(
                    f"Removed {result.stale_mappings_removed} stale state mapping(s)"
                )
            for refusal in result.refusals:
                remaining = " (path remains)" if refusal.path.exists() else ""
                output.warning(
                    f"Left {refusal.repo}: {refusal.path}{remaining}: {refusal.reason}"
                )
            if not result.removed and not result.stale_mappings_removed:
                output.warning("No cleanup candidates were removed")
        else:
            output.json(
                {
                    "pruned": bool(result.count),
                    "count": result.count,
                    "removed": [
                        {
                            "repo": removed.repo,
                            "path": str(removed.path),
                            "recovery_ref": removed.recovery_ref,
                        }
                        for removed in result.removed
                    ],
                    "recovery_refs": result.recovery_refs,
                    "refusals": [
                        {
                            "repo": refusal.repo,
                            "path": str(refusal.path),
                            "reason": refusal.reason,
                        }
                        for refusal in result.refusals
                    ],
                    "remaining_paths": [
                        str(path) for path in result.remaining_paths
                    ],
                    "stale_mappings_removed": result.stale_mappings_removed,
                }
            )

        if result.refusals:
            raise typer.Exit(code=1)
