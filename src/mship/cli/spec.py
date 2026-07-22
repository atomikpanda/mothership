"""`mship spec` sub-app: scaffold and manage structured spec files.

Specs live at `<workspace>/specs/YYYY-MM-DD-<id>.md` — workspace-level,
task-optional, frontmatter-structured. See #145.
"""
from __future__ import annotations

from typing import Optional

import typer

from mship.cli.output import Output


def register(parent: typer.Typer, get_container):
    spec_app = typer.Typer(
        name="spec",
        help="Manage structured specs (`<workspace>/specs/<date>-<id>.md`).",
        no_args_is_help=True,
    )

    def _spec_store():
        """The workspace's spec store in its configured `spec_storage` mode
        (spec-storage-visibility-policy). SpecStore resolves the mode from the
        workspace config by default, so every verb reads + writes through the same
        mode-aware SpecStorage — committed/local/encrypted alike."""
        from pathlib import Path
        from mship.core.spec_store import SPECS_DIRNAME, SpecStore
        container = get_container()
        return SpecStore(Path(container.config_path()).parent / SPECS_DIRNAME)

    @spec_app.command("new")
    def new(
        title: Optional[str] = typer.Option(None, "--title", help="Spec title (required unless --task is given)."),
        spec_id: Optional[str] = typer.Option(None, "--id", help="Stable spec id (slug). Defaults to a slug of the title."),
        task_opt: Optional[str] = typer.Option(None, "--task", help="Link to an existing task: sets task_slug and prefills title + repos."),
        force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing spec file."),
    ):
        """Create a structured spec at `<workspace>/specs/YYYY-MM-DD-<id>.md`."""
        from datetime import datetime, timezone
        from mship.core.spec_draft import new_spec

        container = get_container()
        output = Output()
        store = _spec_store()

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

        now = datetime.now(timezone.utc)
        try:
            spec = new_spec(
                title, now=now, spec_id=spec_id,
                affected_repos=affected_repos, task_slug=task_slug,
            )
        except ValueError:
            output.error("Could not derive a spec id from the title; pass --id explicitly.")
            raise typer.Exit(1)
        path = store.path_for(spec)
        if path.exists() and not force:
            output.error(f"Spec already exists: {path}\n  Pass --force to overwrite.")
            raise typer.Exit(1)
        store.save(spec)

        if output.human_mode:
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
        """Emit a drafting prompt for `<id>` to stdout (run it through your agent, then `spec apply`).

        Without --from-text / --from-file a generic drafting prompt is emitted.
        Supply one of those options to embed your intent directly in the prompt.
        """
        from pathlib import Path
        from mship.core.spec_draft import build_draft_prompt

        output = Output()
        if from_text is not None and from_file is not None:
            output.error("Provide only one of --from-text or --from-file, not both.")
            raise typer.Exit(1)

        store = _spec_store()
        if store.find_by_id(spec_id) is None:
            output.error(f"No spec with id {spec_id!r}. Create it first with `mship spec new`.")
            raise typer.Exit(1)

        if from_text is not None:
            intent = from_text
        elif from_file is not None:
            try:
                intent = Path(from_file).read_text()
            except OSError as e:
                output.error(f"Cannot read --from-file {from_file!r}: {e}")
                raise typer.Exit(1)
        else:
            intent = (
                "<Describe your intent here: the problem to solve, who benefits, "
                "and any constraints. Pass --from-text or --from-file to embed it automatically.>"
            )
        typer.echo(build_draft_prompt(spec_id, intent))

    @spec_app.command("apply")
    def apply(
        spec_id: str = typer.Argument(..., help="Spec id to apply the draft to."),
        from_json: Optional[str] = typer.Option(None, "--from-json", help="Path to the draft JSON, or - for stdin."),
        from_file: Optional[str] = typer.Option(None, "--from-file", help="Path to a rendered spec markdown file, or - for stdin."),
        bypass_status_gate: bool = typer.Option(False, "--bypass-status-gate", help="Apply regardless of current status."),
    ):
        """Ingest a drafted spec, render the body, set fields, advance to needs_review.

        Provide exactly one source:
          --from-json <file|->   a structured SpecDraft JSON payload
          --from-file <file|->   a rendered spec markdown document (## Problem / ## Approach / …)
        Both feed the SAME apply path; only the deserialization differs. Note the
        markdown path recovers only the prose + list sections it renders — the
        JSON path remains authoritative for anything a rendered body omits.
        """
        import json
        import sys
        from datetime import datetime, timezone
        from pathlib import Path
        from pydantic import ValidationError
        from mship.core.spec import SpecDraft, InvalidTransition, validate_transition
        from mship.core.spec_draft import apply_draft, parse_spec_markdown

        output = Output()

        if (from_json is None) == (from_file is None):
            output.error("Provide exactly one of --from-json or --from-file.")
            raise typer.Exit(1)

        source_flag = "--from-json" if from_json is not None else "--from-file"
        source_val = from_json if from_json is not None else from_file
        if source_val == "-":
            raw = sys.stdin.read()
        else:
            try:
                raw = Path(source_val).read_text()
            except OSError as e:
                output.error(f"Cannot read {source_flag} {source_val!r}: {e}")
                raise typer.Exit(1)

        try:
            if from_json is not None:
                draft = SpecDraft(**json.loads(raw))
            else:
                draft = parse_spec_markdown(raw)
        except (json.JSONDecodeError, ValidationError) as e:
            # ValidationError can bubble from SpecDraft(...) on EITHER path, so
            # label by the actual source flag rather than hard-coding "JSON"
            # (a --from-file ValidationError must not read as a JSON error).
            kind = "draft JSON" if from_json is not None else "spec markdown"
            output.error(f"Invalid {kind} from {source_flag}: {e}")
            raise typer.Exit(1)
        except ValueError as e:
            output.error(f"Invalid spec markdown from {source_flag}: {e}")
            raise typer.Exit(1)

        container = get_container()
        store = _spec_store()
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

        # MOS-215/MOS-240: applying a (re)drafted spec supersedes any pending
        # request-changes, so clear the reason — a freshly applied draft carries
        # no outstanding clarification ask. (A brand-new draft has none anyway.)
        apply_draft(spec, draft)
        spec.status = "needs_review"
        spec.clarification_reason = None
        spec.updated_at = datetime.now(timezone.utc)
        path = store.save(spec)

        # Agent-agnostic activity heartbeat: applying a drafted spec is task work.
        # No-ops when the spec's bound task_slug isn't (yet) a live task.
        if spec.task_slug:
            container.state_manager().record_activity(spec.task_slug)

        if output.human_mode:
            output.success(f"Applied draft → {spec.status}: {path}")
        else:
            output.json({"id": spec.id, "status": spec.status, "path": str(path)})

    @spec_app.command("review")
    def review(
        spec_id: str = typer.Argument(..., help="Spec id to review."),
    ):
        """Emit a spec's review units (criteria + questions + read-only context)."""
        from mship.core.spec_review import build_review

        output = Output()
        store = _spec_store()
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)

        payload = build_review(spec)
        if output.human_mode:
            output.print(f"[bold]{payload['id']}[/bold] ({payload['status']})")
            if payload["clarification_reason"]:
                output.print(f"  [bold yellow]Requested changes:[/bold yellow] {payload['clarification_reason']}")
            for c in payload["acceptance_criteria"]:
                output.print(f"  [{c['verdict']}] {c['id']}: {c['text']}")
                for ev in c["evidence"]:
                    note = f" ({ev['note']})" if ev.get("note") else ""
                    output.print(f"      · {ev['kind']}: {ev['ref']}{note}")
            s = payload["summary"]
            output.print(
                f"  summary: {s['approved']} approved, {s['flagged']} flagged, "
                f"{s['unreviewed']} unreviewed, {s['unverified']} unverified; "
                f"{s['open_questions_unanswered']} open question(s)"
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
        from mship.core.spec_review import set_criterion_verdict

        output = Output()
        store = _spec_store()
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
        if output.human_mode:
            output.success(f"{criterion_id} → {verdict_value}: {path}")
        else:
            output.json({"id": spec.id, "criterion": criterion_id, "verdict": verdict_value})

    @spec_app.command("evidence")
    def evidence(
        spec_id: str = typer.Argument(..., help="Spec id."),
        criterion_id: str = typer.Argument(..., help="Acceptance criterion id (e.g. ac1)."),
        ref: str = typer.Argument(
            ..., help="Evidence ref: test-runs/<iter>[.<repo>], a commit sha, or an artifact path/URL.",
        ),
        kind: Optional[str] = typer.Option(
            None, "--kind", help="test | commit | artifact. Inferred from the ref shape when omitted.",
        ),
        note: Optional[str] = typer.Option(None, "--note", help="Optional human note."),
    ):
        """Attach an evidence entry to one acceptance criterion (no status change)."""
        from datetime import datetime, timezone
        from mship.core.spec_review import infer_evidence_kind, set_criterion_evidence

        output = Output()
        store = _spec_store()
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)

        resolved_kind = kind or infer_evidence_kind(ref)
        try:
            set_criterion_evidence(spec, criterion_id, resolved_kind, ref, note)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(1)

        spec.updated_at = datetime.now(timezone.utc)
        path = store.save(spec)
        if output.human_mode:
            output.success(f"{criterion_id} += {resolved_kind}:{ref}: {path}")
        else:
            output.json({"id": spec.id, "criterion": criterion_id, "kind": resolved_kind, "ref": ref})

    @spec_app.command("validate")
    def validate(
        spec_id: str = typer.Argument(..., help="Spec id to validate."),
    ):
        """Check a spec conforms: frontmatter validates + canonical body sections present."""
        from pathlib import Path
        from mship.core.spec_store import SPECS_DIRNAME
        from mship.core.spec_body import validate_body_structure

        output = Output()
        container = get_container()
        workspace_root = Path(container.config_path()).parent
        # Route through the storage layer so an encrypted `.md.enc` spec is decoded
        # and validated, not missed by a raw `*.md` glob (spec-storage-visibility-policy).
        store = _spec_store()
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec file for id {spec_id!r} in {workspace_root / SPECS_DIRNAME}.")
            raise typer.Exit(1)

        missing = validate_body_structure(spec.body)
        if missing:
            output.error(f"{spec_id}: missing body section(s): {', '.join(missing)}")
            raise typer.Exit(1)

        if output.human_mode:
            output.success(f"{spec_id}: valid")
        else:
            output.json({"id": spec_id, "valid": True})

    @spec_app.command("questions")
    def questions(spec_id: str = typer.Argument(..., help="Spec id.")):
        """List a spec's open questions."""
        from mship.core.spec_questions import list_questions
        output = Output()
        store = _spec_store()
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}."); raise typer.Exit(1)
        qs = list_questions(spec)
        if output.human_mode:
            for q in qs:
                output.print(f"  {q['id']}: {q['text']}" + (f"  → {q['answer']}" if q['answer'] else "  (unanswered)"))
        else:
            output.json(qs)

    @spec_app.command("ask")
    def ask(spec_id: str = typer.Argument(...), text: str = typer.Argument(..., help="Question text.")):
        """Add an open question to a spec."""
        from datetime import datetime, timezone
        from mship.core.spec_questions import add_question
        output = Output()
        store = _spec_store()
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}."); raise typer.Exit(1)
        q = add_question(spec, text)
        spec.updated_at = datetime.now(timezone.utc); store.save(spec)
        if output.human_mode:
            output.success(f"Added {q.id}: {text}")
        else:
            output.json({"id": spec.id, "question_id": q.id})

    @spec_app.command("answer")
    def answer(spec_id: str = typer.Argument(...), q_id: str = typer.Argument(...), answer_text: str = typer.Argument(..., metavar="ANSWER")):
        """Answer an open question (does not change status)."""
        from datetime import datetime, timezone
        from mship.core.spec_questions import answer_question
        output = Output()
        store = _spec_store()
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}."); raise typer.Exit(1)
        try:
            answer_question(spec, q_id, answer_text)
        except ValueError as e:
            output.error(str(e)); raise typer.Exit(1)
        spec.updated_at = datetime.now(timezone.utc); store.save(spec)
        if output.human_mode:
            output.success(f"{q_id} answered.")
        else:
            output.json({"id": spec.id, "question_id": q_id})

    @spec_app.command("approve")
    def approve(
        spec_id: str = typer.Argument(...),
        bypass_gate: bool = typer.Option(False, "--bypass-gate", help="Approve despite unmet review gate."),
    ):
        """Approve a spec (gate: all criteria approved + all questions answered)."""
        from mship.core.spec import InvalidTransition
        from mship.core.spec_transition import ApprovalBlocked, approve_spec
        output = Output()
        store = _spec_store()
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)
        try:
            approve_spec(spec, store, bypass_gate=bypass_gate)
        except ApprovalBlocked as e:
            output.error("Cannot approve — " + "; ".join(e.blockers) + ". Use --bypass-gate to override.")
            raise typer.Exit(1)
        except InvalidTransition as e:
            output.error(str(e))
            raise typer.Exit(1)
        path = store.path_for(spec)
        if output.human_mode:
            output.success(f"Approved: {path}")
        else:
            output.json({"id": spec.id, "status": spec.status})

    @spec_app.command("from-thread")
    def from_thread(
        thread_id: str = typer.Argument(..., help="Thread id to draft a spec from."),
        title: Optional[str] = typer.Option(None, "--title", help="Spec title (default: the thread subject)."),
    ):
        """Create a spec seeded from a chat thread's transcript, linked to the thread."""
        from datetime import datetime, timezone
        from pathlib import Path
        from mship.core.message_store import MessageStore
        from mship.core.spec_draft import build_draft_prompt, new_spec

        container = get_container()
        output = Output()
        messages = MessageStore(Path(container.state_dir()) / "messages")
        thread = messages.get(thread_id)
        if thread is None:
            output.error(f"no thread {thread_id!r}")
            raise typer.Exit(1)

        now = datetime.now(timezone.utc)
        spec_store = _spec_store()

        # A thread spawns at most one spec. If from-thread runs again (agent
        # retry, accidental re-invocation), reuse the already-linked spec rather
        # than creating a second one that would orphan the first — there is no
        # back-reference, so a silent overwrite would strand the original draft.
        spec = spec_store.find_by_id(thread.spec_id) if thread.spec_id else None
        reused = spec is not None
        if spec is None:
            spec = new_spec(title or thread.subject.strip() or "captured note", now=now)
            spec_store.save(spec)
            messages.link_spec(thread_id, spec.id, now=now)

        transcript = "\n".join(f"{m.role}: {m.text}" for m in thread.messages)
        verb = "reusing spec" if reused else "created spec"
        output.print(f"{verb} {spec.id!r} linked to thread {thread_id!r}. "
                     f"Run the prompt below, then: mship spec apply {spec.id} --from-json <file>, "
                     f"then mship reply {thread_id} \"drafted {spec.id}\".")
        typer.echo(build_draft_prompt(spec.id, transcript))

    @spec_app.command("dispatch")
    def dispatch(
        spec_id: str = typer.Argument(..., help="Spec id to dispatch (approved, or already dispatched to re-emit)."),
        task_slug: Optional[str] = typer.Option(
            None, "--task",
            help="Bind to this existing task slug instead of auto-spawning a slug==id task.",
        ),
    ):
        """Dispatch an approved spec to a task.

        Picks the task in this order: an explicit `--task <slug>` (must exist);
        else the task this spec is already bound to (idempotent re-dispatch);
        else a task whose slug == spec.id; else auto-spawns one (worktrees per
        the spec's `affected_repos`). Errors if `--task` is unknown or would
        rebind an already-bound spec. Transitions the spec to 'dispatched', sets
        task.spec_id, and prints a handoff prompt with acceptance criteria +
        worktree paths.
        """
        from datetime import datetime, timezone
        from pathlib import Path
        from mship.core.spec_dispatch import DispatchError, dispatch_spec
        from mship.core.workitem_store import WorkItemStore

        output = Output()
        container = get_container()
        workspace_root = Path(container.config_path()).parent
        store = _spec_store()
        workitems = WorkItemStore(workspace_root / ".mothership" / "workitems")
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)

        def _spawn(s):
            return container.worktree_manager().spawn(
                description=s.title,
                repos=list(s.affected_repos),
                slug=s.id,
                workspace_root=workspace_root,
            ).task

        try:
            result = dispatch_spec(
                spec,
                state_manager=container.state_manager(),
                store=store,
                spawn_fn=_spawn,
                now=datetime.now(timezone.utc),
                workitems=workitems,
                workspace=container.config().workspace,
                task_slug=task_slug,
                workspace_root=workspace_root,
                docs_dir=container.config().docs_dir,
            )
        except DispatchError as e:
            output.error(str(e))
            raise typer.Exit(1)

        if output.human_mode and result.spawned:
            output.success(
                f"Auto-spawned task {result.task.slug!r} "
                f"({', '.join(result.task.affected_repos)})."
            )
        typer.echo(result.handoff)

    @spec_app.command("request-changes")
    def request_changes(
        spec_id: str = typer.Argument(...),
        reason: str = typer.Option(..., "--reason", help="What needs to change."),
    ):
        """Send a spec back for changes (→ draft, with a clarification reason).

        MOS-240: `needs_clarification` is gone; "needs clarification" is now the
        non-null `clarification_reason` carried by the editable `draft` status.
        """
        from mship.core.spec import InvalidTransition
        from mship.core.spec_transition import request_changes_spec
        output = Output()
        container = get_container()
        store = _spec_store()
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)
        try:
            request_changes_spec(spec, store, reason)
        except InvalidTransition as e:
            output.error(str(e))
            raise typer.Exit(1)
        # MOS-215: the reason is persisted on the spec (surfaced by `spec
        # show`/`review`) in addition to being journaled to the task log.
        try:
            container.log_manager().append(spec.id, f"spec request-changes: {reason}")
        except Exception:
            pass
        if output.human_mode:
            output.success(f"Requested changes ({spec.status}): {reason}")
        else:
            output.json({"id": spec.id, "status": spec.status, "reason": reason})

    @spec_app.command("list")
    def list_specs():
        """List all specs (TTY: table; non-TTY: JSON envelope)."""
        output = Output()
        store = _spec_store()
        specs = store.list()
        items = [
            {
                "id": s.id,
                "title": s.title,
                "status": s.status,
                "task_slug": s.task_slug,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in specs
        ]
        if output.human_mode:
            from rich.table import Table
            from rich.console import Console
            table = Table(title="Specs")
            for col in ("id", "title", "status", "task_slug", "updated_at"):
                table.add_column(col)
            for item in items:
                table.add_row(
                    item["id"],
                    item["title"],
                    item["status"],
                    item["task_slug"] or "",
                    str(item["updated_at"] or ""),
                )
            Console().print(table)
        else:
            output.json({"specs": items})

    @spec_app.command("show")
    def show_spec(
        spec_id: str = typer.Argument(..., help="Spec id to show."),
    ):
        """Show structured detail for a spec (non-TTY: pure JSON)."""
        output = Output()
        store = _spec_store()
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)
        data = {
            "id": spec.id,
            "title": spec.title,
            "status": spec.status,
            "clarification_reason": spec.clarification_reason,
            "task_slug": spec.task_slug,
            "updated_at": spec.updated_at.isoformat() if spec.updated_at else None,
            "created_at": spec.created_at.isoformat() if spec.created_at else None,
            "affected_repos": spec.affected_repos,
            "acceptance_criteria": [
                {
                    "id": ac.id,
                    "text": ac.text,
                    "verdict": ac.verdict,
                    "done": ac.verdict == "approved",
                }
                for ac in (spec.acceptance_criteria or [])
            ],
            "open_questions": [
                {"id": oq.id, "text": oq.text, "answer": oq.answer}
                for oq in (spec.open_questions or [])
            ],
            "non_goals": spec.non_goals,
            "risks": spec.risks,
            "body": spec.body,
        }
        if output.human_mode:
            from rich.console import Console
            from rich.markdown import Markdown
            console = Console()
            console.print(f"[bold]{spec.title}[/bold]  ({spec.id})")
            console.print(f"Status: {spec.status}  Task: {spec.task_slug or '—'}")
            if spec.clarification_reason:
                console.print(f"[bold yellow]Requested changes:[/bold yellow] {spec.clarification_reason}")
            if spec.body:
                console.print(Markdown(spec.body))
        else:
            output.json(data)

    def _simple_transition(target_status: str, spec_id: str) -> None:
        """Shared logic for implemented/archive: validate transition and save."""
        from datetime import datetime, timezone
        from mship.core.spec import InvalidTransition, validate_transition

        output = Output()
        store = _spec_store()
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)
        try:
            validate_transition(spec.status, target_status)
        except InvalidTransition as e:
            output.error(str(e))
            raise typer.Exit(1)
        spec.status = target_status
        spec.updated_at = datetime.now(timezone.utc)
        store.save(spec)
        if output.human_mode:
            output.success(f"Spec {spec.id} → {target_status}")
        else:
            output.json({"id": spec.id, "status": spec.status})

    @spec_app.command("implemented")
    def mark_implemented(
        spec_id: str = typer.Argument(..., help="Spec id to mark as implemented."),
    ):
        """Advance a dispatched spec to implemented."""
        _simple_transition("implemented", spec_id)

    @spec_app.command("archive")
    def archive(
        spec_id: str = typer.Argument(..., help="Spec id to archive."),
    ):
        """Advance an implemented spec to archived."""
        _simple_transition("archived", spec_id)

    parent.add_typer(spec_app, rich_help_panel="Work items & specs")
