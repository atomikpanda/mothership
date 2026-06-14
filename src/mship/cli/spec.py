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

        if from_text is not None:
            intent = from_text
        else:
            try:
                intent = Path(from_file).read_text()
            except OSError as e:
                output.error(f"Cannot read --from-file {from_file!r}: {e}")
                raise typer.Exit(1)
        typer.echo(build_draft_prompt(spec_id, intent))

    @spec_app.command("apply")
    def apply(
        spec_id: str = typer.Argument(..., help="Spec id to apply the draft to."),
        from_json: str = typer.Option(..., "--from-json", help="Path to the draft JSON, or - for stdin."),
        bypass_status_gate: bool = typer.Option(False, "--bypass-status-gate", help="Apply regardless of current status."),
    ):
        """Ingest a SpecDraft JSON: render the body, set fields, advance to needs_review."""
        import json
        import sys
        from datetime import datetime, timezone
        from pathlib import Path
        from pydantic import ValidationError
        from mship.core.spec import SpecDraft, InvalidTransition, validate_transition
        from mship.core.spec_draft import apply_draft
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME

        output = Output()
        if from_json == "-":
            raw = sys.stdin.read()
        else:
            try:
                raw = Path(from_json).read_text()
            except OSError as e:
                output.error(f"Cannot read --from-json {from_json!r}: {e}")
                raise typer.Exit(1)
        try:
            draft = SpecDraft(**json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            output.error(f"Invalid draft JSON: {e}")
            raise typer.Exit(1)

        container = get_container()
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)

        if not bypass_status_gate:
            try:
                validate_transition(spec.status, "needs_review")
            except InvalidTransition as e:
                output.error(f"{e}. Use --bypass-status-gate to override.")
                raise typer.Exit(1)

        apply_draft(spec, draft)
        spec.status = "needs_review"
        spec.updated_at = datetime.now(timezone.utc)
        path = store.save(spec)

        if output.is_tty:
            output.success(f"Applied draft → {spec.status}: {path}")
        else:
            output.json({"id": spec.id, "status": spec.status, "path": str(path)})

    @spec_app.command("review")
    def review(
        spec_id: str = typer.Argument(..., help="Spec id to review."),
    ):
        """Emit a spec's review units (criteria + questions + read-only context)."""
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME
        from mship.core.spec_review import build_review

        output = Output()
        container = get_container()
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)

        payload = build_review(spec)
        if output.is_tty:
            output.print(f"[bold]{payload['id']}[/bold] ({payload['status']})")
            for c in payload["acceptance_criteria"]:
                output.print(f"  [{c['verdict']}] {c['id']}: {c['text']}")
            s = payload["summary"]
            output.print(
                f"  summary: {s['approved']} approved, {s['flagged']} flagged, "
                f"{s['unreviewed']} unreviewed; {s['open_questions_unanswered']} open question(s)"
            )
        else:
            output.json(payload)

    @spec_app.command("verdict")
    def verdict(
        spec_id: str = typer.Argument(..., help="Spec id."),
        criterion_id: str = typer.Argument(..., help="Acceptance criterion id (e.g. ac1)."),
        verdict_value: str = typer.Argument(..., metavar="VERDICT", help="unreviewed | approved | flagged."),
    ):
        """Record a verdict on one acceptance criterion (no status change)."""
        from datetime import datetime, timezone
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME
        from mship.core.spec_review import set_criterion_verdict

        output = Output()
        container = get_container()
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)

        try:
            set_criterion_verdict(spec, criterion_id, verdict_value)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(1)

        spec.updated_at = datetime.now(timezone.utc)
        path = store.save(spec)
        if output.is_tty:
            output.success(f"{criterion_id} → {verdict_value}: {path}")
        else:
            output.json({"id": spec.id, "criterion": criterion_id, "verdict": verdict_value})

    @spec_app.command("validate")
    def validate(
        spec_id: str = typer.Argument(..., help="Spec id to validate."),
    ):
        """Check a spec conforms: frontmatter validates + canonical body sections present."""
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME, SpecParseError, parse_spec
        from mship.core.spec_body import validate_body_structure

        output = Output()
        container = get_container()
        workspace_root = Path(container.config_path()).parent
        specs_dir = workspace_root / SPECS_DIRNAME

        matches = sorted(specs_dir.glob(f"*-{spec_id}.md"))
        if not matches:
            output.error(f"No spec file for id {spec_id!r} in {specs_dir}.")
            raise typer.Exit(1)

        try:
            spec = parse_spec(matches[0].read_text())
        except SpecParseError as e:
            output.error(f"{spec_id}: invalid frontmatter — {e}")
            raise typer.Exit(1)

        if spec.id != spec_id:
            output.error(f"{spec_id}: file {matches[0].name} has mismatched id {spec.id!r}.")
            raise typer.Exit(1)

        missing = validate_body_structure(spec.body)
        if missing:
            output.error(f"{spec_id}: missing body section(s): {', '.join(missing)}")
            raise typer.Exit(1)

        if output.is_tty:
            output.success(f"{spec_id}: valid")
        else:
            output.json({"id": spec_id, "valid": True})

    @spec_app.command("questions")
    def questions(spec_id: str = typer.Argument(..., help="Spec id.")):
        """List a spec's open questions."""
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME
        from mship.core.spec_questions import list_questions
        output = Output(); container = get_container()
        store = SpecStore(Path(container.config_path()).parent / SPECS_DIRNAME)
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}."); raise typer.Exit(1)
        qs = list_questions(spec)
        if output.is_tty:
            for q in qs:
                output.print(f"  {q['id']}: {q['text']}" + (f"  → {q['answer']}" if q['answer'] else "  (unanswered)"))
        else:
            output.json(qs)

    @spec_app.command("ask")
    def ask(spec_id: str = typer.Argument(...), text: str = typer.Argument(..., help="Question text.")):
        """Add an open question to a spec."""
        from datetime import datetime, timezone
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME
        from mship.core.spec_questions import add_question
        output = Output(); container = get_container()
        store = SpecStore(Path(container.config_path()).parent / SPECS_DIRNAME)
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}."); raise typer.Exit(1)
        q = add_question(spec, text)
        spec.updated_at = datetime.now(timezone.utc); store.save(spec)
        if output.is_tty:
            output.success(f"Added {q.id}: {text}")
        else:
            output.json({"id": spec.id, "question_id": q.id})

    @spec_app.command("answer")
    def answer(spec_id: str = typer.Argument(...), q_id: str = typer.Argument(...), answer_text: str = typer.Argument(..., metavar="ANSWER")):
        """Answer an open question (does not change status)."""
        from datetime import datetime, timezone
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME
        from mship.core.spec_questions import answer_question
        output = Output(); container = get_container()
        store = SpecStore(Path(container.config_path()).parent / SPECS_DIRNAME)
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}."); raise typer.Exit(1)
        try:
            answer_question(spec, q_id, answer_text)
        except ValueError as e:
            output.error(str(e)); raise typer.Exit(1)
        spec.updated_at = datetime.now(timezone.utc); store.save(spec)
        if output.is_tty:
            output.success(f"{q_id} answered.")
        else:
            output.json({"id": spec.id, "question_id": q_id})

    parent.add_typer(spec_app)
