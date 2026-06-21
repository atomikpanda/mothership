"""`mship capture` — capture the running UI for an agent to inspect."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output
from mship.core import capture as _cap
from mship.core.dispatch import resolve_repo
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)


def register(app: typer.Typer, get_container):
    @app.command()
    def capture(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug (defaults to cwd-resolved)."),
        repo: Optional[str] = typer.Option(None, "--repo", help="Which repo to capture (required for an ad-hoc capture when the workspace has >1 repo)."),
        platform: Optional[str] = typer.Option(None, "--platform", help="Platform to capture (required when the repo exposes more than one)."),
        kind: str = typer.Option("all", "--kind", help="Artifact kind: image | layout | all."),
        out: Optional[Path] = typer.Option(None, "--out", help="Output directory (default: .mothership/captures/<task-or-_adhoc>/<ts>-<platform>/)."),
    ):
        """Capture the running UI (screenshot + layout) into files to read.

        Task-aware but not task-required: with an active task, captures run in the
        task's worktree and are filed under the task. Without one, capture runs an
        ad-hoc capture against a repo's main checkout — capture observes a running
        app, not worktree source, so it shouldn't require a task.
        """
        output = Output()
        container = get_container()
        state = container.state_manager().load()
        config = container.config()

        try:
            kinds = _cap.resolve_kinds(kind)
        except _cap.CaptureError as e:
            output.error(str(e))
            raise typer.Exit(code=2)

        # Resolve a task if one is anchored. No active task -> ad-hoc capture
        # against a repo's main checkout. Ambiguous/unknown still error: the user
        # clearly has tasks and should disambiguate rather than silently fall back.
        try:
            t, source = resolve_task(
                state, cli_task=task,
                env_task=os.environ.get("MSHIP_TASK"), cwd=Path.cwd(),
            )
        except NoActiveTaskError:
            t, source = None, None
        except (AmbiguousTaskError, UnknownTaskError) as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if t is not None:
            output.breadcrumb(f"→ task: {t.slug}  (resolved via {source.value})")
            try:
                resolved_repo = resolve_repo(t, repo)
            except ValueError as e:
                output.error(str(e))
                raise typer.Exit(code=1)
            worktree = Path(t.worktrees[resolved_repo])
            out_bucket = t.slug
        else:
            try:
                resolved_repo = _cap.resolve_adhoc_repo(list(config.repos), repo)
            except _cap.CaptureError as e:
                output.error(str(e))
                raise typer.Exit(code=1)
            # repo.path is resolved to an absolute main-checkout path at load time.
            worktree = Path(config.repos[resolved_repo].path)
            out_bucket = "_adhoc"

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

        if out is not None:
            out_dir = out
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            workspace_root = Path(container.config_path()).parent
            label = resolved_platform or "default"
            out_dir = workspace_root / ".mothership" / "captures" / out_bucket / f"{ts}-{label}"

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
                "resolved_task": t.slug if t is not None else None,
                "resolution_source": source.value if source is not None else None,
            })
