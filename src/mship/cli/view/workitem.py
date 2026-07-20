"""`mship view workitem <id>` — single-WorkItem cockpit on the master/detail base.

Thin wiring: `build_rows` maps a pure `WorkItemCockpit` (assembled in
core/view/workitem_cockpit) to `ListRow`s, and `WorkItemCockpitView` renders them
on the reusable `MasterDetailApp`. The store resolver + Typer command are added in
the next task.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli.view._master_detail import ListRow, MasterDetailApp
from mship.core.view.workitem_cockpit import (
    WorkItemCockpit, criterion_detail, pr_detail, spec_detail, task_detail,
    thread_detail,
)


def build_rows(cockpit: WorkItemCockpit) -> list[ListRow]:
    """One flat, sectioned list of the WorkItem's entities: the spec, each
    acceptance criterion, each task, each PR, each thread — each row carrying its
    own pre-rendered detail (reusing the shared cockpit formatters)."""
    rows: list[ListRow] = [
        ListRow(
            key="spec",
            label=f"spec  {cockpit.spec_id or '(none)'}  [{cockpit.spec_status or '—'}]",
            detail=spec_detail(cockpit),
        )
    ]
    for c in cockpit.criteria:
        rows.append(ListRow(
            key=f"ac:{c.id}",
            label=f"{c.id}  [{c.verdict}]  {c.text}",
            detail=criterion_detail(c),
        ))
    for t in cockpit.tasks:
        rows.append(ListRow(
            key=f"task:{t.slug}",
            label=f"task  {t.slug}  [{t.phase}]",
            detail=task_detail(t),
        ))
    for p in cockpit.prs:
        rows.append(ListRow(
            key=f"pr:{p.task_slug}:{p.repo}",
            label=f"PR  {p.repo}  ({p.task_slug})",
            detail=pr_detail(p),
        ))
    for th in cockpit.threads:
        rows.append(ListRow(
            key=f"thread:{th.id}",
            label=f"thread  {th.subject}",
            detail=thread_detail(th),
        ))
    return rows


class WorkItemCockpitView(MasterDetailApp):
    def __init__(self, cockpit: WorkItemCockpit, **kw) -> None:
        super().__init__(**kw)
        self._cockpit = cockpit

    def list_rows(self) -> list[ListRow]:
        return build_rows(self._cockpit)

    def header_line(self) -> str | None:
        return f"◆ {self._cockpit.id}  ·  {self._cockpit.title}  ·  [{self._cockpit.phase}]"


def _resolve_cockpit(container, item_id: str) -> WorkItemCockpit | None:
    """Resolve one WorkItem's cockpit from the canonical stores, or None if the id
    is unknown. Single entry point is the `WorkItemSummary` (from PR1's
    load_workitem_index): it carries the derived phase + spec_id/task_slugs/
    thread_ids used to fetch the linked spec, tasks, and threads."""
    from mship.cli.view._workitems import load_workitem_index
    from mship.core.message_store import MessageStore
    from mship.core.spec_store import SPECS_DIRNAME, SpecStore
    from mship.core.view.workitem_cockpit import assemble_cockpit

    summary = next((s for s in load_workitem_index(container) if s.id == item_id), None)
    if summary is None:
        return None

    workspace_root = Path(container.config_path()).parent
    state_dir = Path(container.state_dir())

    spec = None
    if summary.spec_id:
        spec = SpecStore(workspace_root / SPECS_DIRNAME).find_by_id(summary.spec_id)

    state = container.state_manager().load()
    tasks = [state.tasks[s] for s in summary.task_slugs if s in state.tasks]

    msgs = MessageStore(state_dir / "messages")
    threads = [th for th in (msgs.get(tid) for tid in summary.thread_ids) if th is not None]

    return assemble_cockpit(summary, spec, tasks, threads)


def register(app: "typer.Typer", get_container):
    @app.command()
    def workitem(
        item_id: str = typer.Argument(..., help="WorkItem id to open (e.g. wi-...)"),
    ):
        """Single-WorkItem cockpit: spec (status + phase), acceptance criteria with
        evidence, tasks + worktrees, and linked PRs + threads."""
        from mship.cli.output import Output
        from mship.core.view.workitem_cockpit import render_text

        container = get_container()
        cockpit = _resolve_cockpit(container, item_id)
        if cockpit is None:
            typer.echo(f"Error: unknown work item: {item_id}", err=True)
            raise typer.Exit(code=1)

        # Non-TTY short-circuit (mirrors `mship view spec` #124): the Textual TUI
        # hangs when stdout isn't a terminal (agent pipes, CI, CliRunner). Print
        # the flat cockpit text and exit instead.
        if not Output().is_tty:
            typer.echo(render_text(cockpit))
            return

        WorkItemCockpitView(cockpit).run()
