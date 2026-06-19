"""`mship capture` — capture the running UI for an agent to inspect."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core import capture as _cap
from mship.core.dispatch import resolve_repo


def register(app: typer.Typer, get_container):
    @app.command()
    def capture(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug (defaults to cwd-resolved)."),
        repo: Optional[str] = typer.Option(None, "--repo", help="Which repo's worktree to capture (multi-repo tasks)."),
        platform: Optional[str] = typer.Option(None, "--platform", help="Platform to capture (required when the repo exposes more than one)."),
        kind: str = typer.Option("all", "--kind", help="Artifact kind: image | layout | all."),
        out: Optional[Path] = typer.Option(None, "--out", help="Output directory (default: .mothership/captures/<task>/<ts>-<platform>/)."),
    ):
        """Capture the running UI (screenshot + layout) into files to read."""
        output = Output()
        container = get_container()
        state = container.state_manager().load()
        resolved = resolve_for_command("capture", state, task, output)
        t = resolved.task

        try:
            resolved_repo = resolve_repo(t, repo)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        try:
            kinds = _cap.resolve_kinds(kind)
        except _cap.CaptureError as e:
            output.error(str(e))
            raise typer.Exit(code=2)

        config = container.config()
        repo_cfg = config.repos[resolved_repo]
        platforms = repo_cfg.capture.platforms if repo_cfg.capture else []

        resolved_platform = platform
        if resolved_platform is None:
            if len(platforms) == 1:
                resolved_platform = platforms[0]
            elif len(platforms) > 1:
                output.error(
                    f"--platform is required for repo {resolved_repo!r}; "
                    f"choose one of: {', '.join(platforms)}."
                )
                raise typer.Exit(code=2)
        elif platforms and resolved_platform not in platforms:
            output.error(
                f"unknown platform {resolved_platform!r} for repo {resolved_repo!r}; "
                f"choose one of: {', '.join(platforms)}."
            )
            raise typer.Exit(code=2)

        actual = repo_cfg.tasks.get("capture", "capture")
        worktree = Path(t.worktrees[resolved_repo])

        if out is not None:
            out_dir = out
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            workspace_root = Path(container.config_path()).parent
            label = resolved_platform or "default"
            out_dir = workspace_root / ".mothership" / "captures" / t.slug / f"{ts}-{label}"

        try:
            artifacts = _cap.run_capture(
                shell=container.shell(),
                worktree=worktree,
                actual_task_name=actual,
                env_runner=repo_cfg.env_runner,
                platform=resolved_platform,
                kinds=kinds,
                out_dir=out_dir,
            )
        except _cap.CaptureError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if output.is_tty:
            for a in artifacts:
                output.success(f"captured {a.kind} → {a.path}")
        else:
            output.json({
                "platform": resolved_platform,
                "repo": resolved_repo,
                "artifacts": [{"kind": a.kind, "path": str(a.path)} for a in artifacts],
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })
