"""`mship dispatch` — emit an agent-agnostic subagent prompt to stdout.

See docs/superpowers/specs/2026-04-17-mship-dispatch-design.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core import dispatch as _d
from mship.core.skill_install import pkg_skills_source


def register(app: typer.Typer, get_container):
    @app.command()
    def dispatch(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug (defaults to cwd-resolved)."),
        repo: Optional[str] = typer.Option(None, "--repo", help="Which repo's worktree to target (multi-repo tasks)."),
        instruction: str = typer.Option(..., "--instruction", "-i", help="Instruction text passed verbatim to the subagent."),
    ):
        """Emit a self-contained markdown subagent prompt to stdout."""
        output = Output()
        container = get_container()
        state = container.state_manager().load()
        resolved = resolve_for_command("dispatch", state, task, output)
        task_obj = resolved.task

        try:
            resolved_repo = _d.resolve_repo(task_obj, repo)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        worktree = Path(task_obj.worktrees[resolved_repo])
        base_sha_info = _d.collect_base_sha_info(worktree, task_obj.base_branch or "main")

        log_mgr = container.log_manager()
        journal_entries = log_mgr.read(task_obj.slug, last=10)

        # AGENTS.md lives next to the config file (workspace root).
        config_path = Path(container.config_path())
        agents_md = config_path.parent / "AGENTS.md"
        agents_md_path = agents_md if agents_md.is_file() else None

        prompt = _d.build_dispatch_prompt(
            task=task_obj,
            repo=resolved_repo,
            instruction=instruction,
            journal_entries=journal_entries,
            base_sha_info=base_sha_info,
            agents_md_path=agents_md_path,
            pkg_skills_source=pkg_skills_source(),
        )
        # Print directly to stdout (NOT via Output.json — this is meant to be piped).
        print(prompt)
