"""`mship spec` sub-app: scaffold and manage per-task spec files.

The blessed location is `<workspace>/.mothership/tasks/<slug>/SPEC.md` —
mship-private, task-scoped, easy for the dev-phase gate to check. See #126.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


SPEC_TEMPLATE = """\
# Spec: {slug}

**Description:** {description}
**Created:** {created}
**Affected repos:** {repos}

## Goals

_What outcome does this task produce? Why does it matter?_

## Approach

_How will the implementation work? What are the key decisions?_

## Acceptance criteria

- [ ] _what success looks like, in user-visible terms_
"""


def blessed_spec_path(state_dir: Path, slug: str) -> Path:
    """The mship-blessed task-scoped spec location."""
    return state_dir / "tasks" / slug / "SPEC.md"


def render_template(slug: str, description: str, repos: list[str]) -> str:
    return SPEC_TEMPLATE.format(
        slug=slug,
        description=description,
        created=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        repos=", ".join(repos) if repos else "(none)",
    )


def register(parent: typer.Typer, get_container):
    spec_app = typer.Typer(
        name="spec",
        help="Manage per-task specs (`.mothership/tasks/<slug>/SPEC.md`).",
        no_args_is_help=True,
    )

    @spec_app.command("new")
    def new(
        force: bool = typer.Option(
            False, "--force", "-f",
            help="Overwrite an existing spec at the blessed path.",
        ),
        task_opt: Optional[str] = typer.Option(
            None, "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var.",
        ),
    ):
        """Scaffold a stub spec at `.mothership/tasks/<slug>/SPEC.md`."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        resolved = resolve_for_command("spec new", state, task_opt, output)
        t = resolved.task

        state_dir = container.state_dir()
        path = blessed_spec_path(state_dir, t.slug)
        if path.exists() and not force:
            output.error(
                f"Spec already exists: {path}\n"
                f"  Pass --force to overwrite, or `mship view spec` to read it."
            )
            raise typer.Exit(code=1)

        body = render_template(t.slug, t.description, list(t.affected_repos))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

        if output.is_tty:
            output.success(f"Created spec: {path}")
            output.print("[dim]Edit the file, then `mship phase dev` to start work.[/dim]")
        else:
            output.json({
                "task": t.slug,
                "path": str(path),
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })

    parent.add_typer(spec_app)
