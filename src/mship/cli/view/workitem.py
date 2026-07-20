"""`mship view workitem <id>` — single-WorkItem cockpit on the master/detail base.

Thin wiring: `build_rows` maps a pure `WorkItemCockpit` (assembled in
core/view/workitem_cockpit) to `ListRow`s, and `WorkItemCockpitView` renders them
on the reusable `MasterDetailApp`. The store resolver + Typer command are added in
the next task.
"""
from __future__ import annotations

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
