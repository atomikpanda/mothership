"""`mship spec` sub-app: scaffold and manage structured spec files.

Specs live at `<workspace>/specs/YYYY-MM-DD-<id>.md` — workspace-level,
task-optional, frontmatter-structured. See #145.
"""
from __future__ import annotations

from typing import Optional

import typer

from mship.cli.output import Output


SPEC_BODY_TEMPLATE = """\
## Problem

_What problem does this solve? Why now?_

## User story

_As a <user>, I want <capability>, so that <benefit>._

## Approach

_How will it work? Key decisions._
"""


def register(parent: typer.Typer, get_container):
    spec_app = typer.Typer(
        name="spec",
        help="Manage structured specs (`<workspace>/specs/<date>-<id>.md`).",
        no_args_is_help=True,
    )

    @spec_app.command("new")
    def new(
        title: Optional[str] = typer.Option(None, "--title", help="Spec title (required unless --task is given)."),
        spec_id: Optional[str] = typer.Option(None, "--id", help="Stable spec id (slug). Defaults to a slug of the title."),
        task_opt: Optional[str] = typer.Option(None, "--task", help="Link to an existing task: sets task_slug and prefills title + repos."),
        force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing spec file."),
    ):
        """Create a structured spec at `<workspace>/specs/YYYY-MM-DD-<id>.md`."""
        from datetime import datetime, timezone
        from pathlib import Path
        from mship.core.spec import Spec
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME
        from mship.util.slug import slugify

        container = get_container()
        output = Output()
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)

        affected_repos: list[str] = []
        task_slug: Optional[str] = None
        if task_opt is not None:
            state = container.state_manager().load()
            if task_opt not in state.tasks:
                known = ", ".join(sorted(state.tasks)) or "(none)"
                output.error(f"Unknown task: {task_opt}. Known: {known}.")
                raise typer.Exit(1)
            t = state.tasks[task_opt]
            affected_repos = list(t.affected_repos)
            task_slug = t.slug
            if title is None:
                title = t.description
            if spec_id is None:
                spec_id = t.slug

        if title is None:
            output.error("Provide --title (or --task to derive it).")
            raise typer.Exit(1)
        if spec_id is None:
            spec_id = slugify(title)
        if not spec_id:
            output.error("Could not derive a spec id from the title; pass --id explicitly.")
            raise typer.Exit(1)

        now = datetime.now(timezone.utc)
        spec = Spec(
            id=spec_id, title=title, status="drafting",
            created_at=now, updated_at=now,
            affected_repos=affected_repos, task_slug=task_slug,
            body=SPEC_BODY_TEMPLATE,
        )
        path = store.path_for(spec)
        if path.exists() and not force:
            output.error(f"Spec already exists: {path}\n  Pass --force to overwrite.")
            raise typer.Exit(1)
        store.save(spec)

        if output.is_tty:
            output.success(f"Created spec: {path}")
            output.print("[dim]Edit the prose; lifecycle commands (draft/review/approve) follow.[/dim]")
        else:
            output.json({
                "id": spec.id, "path": str(path),
                "status": spec.status, "task_slug": task_slug,
            })

    @spec_app.command("draft")
    def draft(
        spec_id: str = typer.Argument(..., help="Spec id to draft (must already exist)."),
        from_text: Optional[str] = typer.Option(None, "--from-text", help="Inline intent text."),
        from_file: Optional[str] = typer.Option(None, "--from-file", help="Read intent from a file."),
    ):
        """Emit a drafting prompt for `<id>` to stdout (run it through your agent, then `spec apply`)."""
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME
        from mship.core.spec_draft import build_draft_prompt

        output = Output()
        if (from_text is None) == (from_file is None):
            output.error("Provide exactly one of --from-text or --from-file.")
            raise typer.Exit(1)

        container = get_container()
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)
        if store.find_by_id(spec_id) is None:
            output.error(f"No spec with id {spec_id!r}. Create it first with `mship spec new`.")
            raise typer.Exit(1)

        intent = from_text if from_text is not None else Path(from_file).read_text()
        typer.echo(build_draft_prompt(spec_id, intent))

    parent.add_typer(spec_app)
