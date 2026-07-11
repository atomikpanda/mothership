"""`mship export` — assemble a task's journal/plan/spec/state/diffs into a
portable bundle, with opt-in `--redacted` secret scrubbing. See spec
`mship-export-redacted-secret-redaction-mos-102` (MOS-102).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(name="export")
    def export_cmd(
        redacted: bool = typer.Option(
            False, "--redacted",
            help="Strip well-known secret shapes (Stripe/GitHub/AWS/PEM/Bearer/.env-style) "
                 "from the bundle's text artifacts. Opt-in; omitted means a faithful, "
                 "unredacted copy. Built-in patterns are vetted and safe. A custom pattern "
                 "from redact.patterns is your own regex: a pathological one (catastrophic "
                 "backtracking) can still hang export — Python can't forcibly interrupt a "
                 "runaway re.sub, so keep custom patterns simple.",
        ),
        fmt: str = typer.Option(
            "dir", "--format",
            help="Bundle format: 'dir' (default, writes <task>-export/) or 'zip' "
                 "(writes <task>-export.zip).",
        ),
        task_opt: Optional[str] = typer.Option(
            None, "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env > only active task.",
        ),
    ):
        """Assemble <task>'s journal, plan, spec, state, and per-repo diffs into a bundle."""
        from mship.core.export import export_task
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME

        output = Output()
        if fmt not in ("dir", "zip"):
            output.error("Invalid --format: use 'dir' or 'zip'.")
            raise typer.Exit(1)

        container = get_container()
        state = container.state_manager().load()
        resolved = resolve_for_command("export", state, task_opt, output)
        task = resolved.task

        workspace_root = Path(container.config_path()).parent
        spec_store = SpecStore(workspace_root / SPECS_DIRNAME)

        try:
            result = export_task(
                task=task,
                config=container.config(),
                workspace_root=workspace_root,
                log_manager=container.log_manager(),
                spec_store=spec_store,
                redacted=redacted,
                format=fmt,
            )
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(1)

        for warning in result.warnings:
            output.warning(warning)

        if output.human_mode:
            suffix = " (redacted)" if redacted else ""
            output.success(f"Exported {task.slug} → {result.bundle_path}{suffix}")
        else:
            output.json({
                "task": task.slug,
                "bundle_path": str(result.bundle_path),
                "format": fmt,
                "redacted": redacted,
                "warnings": result.warnings,
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })
