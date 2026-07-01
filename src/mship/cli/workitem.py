from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer

from mship.core.message_store import MessageStore
from mship.core.spec_store import SPECS_DIRNAME, SpecStore
from mship.core.workitem import ExternalLink
from mship.core.workitem_store import WorkItemStore
from mship.core.view.workitem_index import build_workitem_index


def register(parent: typer.Typer, get_container) -> None:
    item_app = typer.Typer(help="First-class work items (the phase-aware cockpit spine).")
    parent.add_typer(item_app, name="item")

    def _ctx():
        container = get_container()
        state_dir = Path(container.state_dir())
        workspace_root = Path(container.config_path()).parent
        return (
            WorkItemStore(state_dir / "workitems"),
            SpecStore(workspace_root / SPECS_DIRNAME),
            container.state_manager(),
            MessageStore(state_dir / "messages"),
            container.config().workspace,
        )

    @item_app.command("new")
    def new(title: str, kind: str = typer.Option("feature", "--kind",
            help="feature | bug | chore | question")):
        items, _, _, _, workspace = _ctx()
        wi = items.create(title=title, kind=kind, workspace=workspace,
                          now=datetime.now(timezone.utc))
        typer.echo(wi.id)

    @item_app.command("list")
    def list_items():
        items, specs, state_manager, msgs, _ = _ctx()
        summaries = build_workitem_index(
            items.list(),
            {s.id: s for s in specs.list()},
            dict(state_manager.load().tasks),
            {t.id: t for t in msgs.list()},
        )
        rows = [{"id": s.id, "title": s.title, "kind": s.kind, "phase": s.phase,
                 "needs_approval": s.attention.needs_approval,
                 "needs_decision": s.attention.needs_decision,
                 "blocked": s.attention.blocked, "needs_review": s.attention.needs_review}
                for s in summaries]
        if sys.stdout.isatty():
            for r in rows:
                flags = "".join(k[0].upper() for k in
                                ("needs_approval", "needs_decision", "blocked", "needs_review")
                                if r[k])
                typer.echo(f"{r['id']}  [{r['phase']}]  {r['title']}  {flags}")
            if not rows:
                typer.echo("(no work items)")
        else:
            typer.echo(json.dumps(rows))

    @item_app.command("show")
    def show(item_id: str):
        items, _, _, _, _ = _ctx()
        wi = items.get(item_id)
        if wi is None:
            typer.echo(f"no work item {item_id!r}", err=True)
            raise typer.Exit(1)
        typer.echo(wi.model_dump_json(indent=2))

    @item_app.command("link-spec")
    def link_spec(item_id: str, spec_id: str):
        items, _, _, _, _ = _ctx()
        _guard(items, item_id)
        items.link_spec(item_id, spec_id, now=datetime.now(timezone.utc))
        typer.echo(f"linked spec {spec_id} -> {item_id}")

    @item_app.command("link-task")
    def link_task(item_id: str, task_slug: str):
        items, _, _, _, _ = _ctx()
        _guard(items, item_id)
        items.add_task(item_id, task_slug, now=datetime.now(timezone.utc))
        typer.echo(f"linked task {task_slug} -> {item_id}")

    @item_app.command("link-url")
    def link_url(item_id: str, url: str,
                 provider: str = typer.Option("url", "--provider"),
                 title: str = typer.Option("", "--title")):
        items, _, _, _, _ = _ctx()
        _guard(items, item_id)
        items.add_external_link(item_id, ExternalLink(provider=provider, url=url, title=title),
                                now=datetime.now(timezone.utc))
        typer.echo(f"linked {provider} url -> {item_id}")

    @item_app.command("phase")
    def phase(item_id: str, phase: str):
        items, _, _, _, _ = _ctx()
        _guard(items, item_id)
        items.set_phase_override(item_id, phase, now=datetime.now(timezone.utc))
        typer.echo(f"set phase_override={phase} on {item_id}")

    @item_app.command("migrate")
    def migrate():
        from mship.core.workitem_migrate import wrap_existing
        items, specs, state_manager, msgs, _ = _ctx()
        created = wrap_existing(items, specs, state_manager, msgs, now=datetime.now(timezone.utc))
        typer.echo(f"created {len(created)} work item(s)")

    def _guard(items: WorkItemStore, item_id: str) -> None:
        if items.get(item_id) is None:
            typer.echo(f"no work item {item_id!r}", err=True)
            raise typer.Exit(1)
