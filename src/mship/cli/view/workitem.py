"""`mship view workitem <id>` — single-WorkItem cockpit on the master/detail base.

Thin wiring: `build_rows` maps a pure `WorkItemCockpit` (assembled in
core/view/workitem_cockpit) to `ListRow`s, and `WorkItemCockpitView` renders them
on the reusable `MasterDetailApp`.

PR4 adds the curated inline actions: `a`/`R` on the spec row (when its status is
needs_review) approve / request-changes it through the shared core.spec_transition
seam; `y` copies the selected entity's id/branch/PR-url; `o` opens a PR row's url;
`enter` opens the linked spec in-process. The spec row relabels to the new status
after a successful write.
"""
from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Optional

import typer
from textual import work

from mship.cli.view._master_detail import ListRow, MasterDetailApp
from mship.cli.view._modals import EntityScreen, RequestChangesModal
from mship.core.view.actions import approve_spec_by_id, request_changes_by_id
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
    def __init__(self, cockpit: WorkItemCockpit | None, spec_store=None, **kw) -> None:
        super().__init__(**kw)
        self._cockpit = cockpit
        self._spec_store = spec_store
        self._spec_status = cockpit.spec_status if cockpit is not None else None

    def list_rows(self) -> list[ListRow]:
        if self._cockpit is None:
            from mship.cli.view._follow import follow_hint
            return [ListRow(key="hint", label=follow_hint(), detail=follow_hint())]
        rows = build_rows(self._cockpit)
        # Reflect the live spec status (relabels after an in-view approve / request-changes).
        if rows and self._spec_status is not None:
            rows[0] = ListRow(
                key="spec",
                label=f"spec  {self._cockpit.spec_id or '(none)'}  [{self._spec_status}]",
                detail=rows[0].detail,
            )
        return rows

    def header_line(self) -> str | None:
        if self._cockpit is None:
            from mship.cli.view._follow import follow_hint
            return follow_hint()
        return f"◆ {self._cockpit.id}  ·  {self._cockpit.title}  ·  [{self._cockpit.phase}]"

    def _selected_task(self):
        key = self.selected_key() or ""
        if not key.startswith("task:"):
            return None
        slug = key[len("task:"):]
        return next((t for t in self._cockpit.tasks if t.slug == slug), None)

    def _selected_pr(self):
        key = self.selected_key() or ""
        return next((p for p in self._cockpit.prs
                     if f"pr:{p.task_slug}:{p.repo}" == key), None)

    # AC7 — approve / request-changes (only on the spec row when needs_review) --
    def _do_approve(self) -> None:
        if self.selected_key() != "spec" or self._spec_status != "needs_review" or self._spec_store is None:
            self._announce("Approve applies to the spec row while it awaits review.")
            return
        out = approve_spec_by_id(self._spec_store, self._cockpit.spec_id)
        self._announce(out.message)
        if out.ok:
            self._spec_status = "approved"
            self.call_later(self.reload_rows)

    @work
    async def _do_request_changes(self) -> None:
        if self.selected_key() != "spec" or self._spec_status != "needs_review" or self._spec_store is None:
            self._announce("Request-changes applies to the spec row while it awaits review.")
            return
        reason = await self.push_screen_wait(RequestChangesModal(self._cockpit.spec_id))
        if reason is None:
            self._announce("Request-changes cancelled.")
            return
        out = request_changes_by_id(self._spec_store, self._cockpit.spec_id, reason)
        self._announce(out.message)
        if out.ok:
            self._spec_status = "draft"
            await self.reload_rows()

    # AC8 — open / copy ---------------------------------------------------------
    def _do_open_external(self) -> None:
        pr = self._selected_pr()
        if pr is not None and pr.url:
            webbrowser.open(pr.url)
            self._announce(f"Opened {pr.url}")
        else:
            self._announce("No PR to open on this row.")

    def _do_copy(self) -> None:
        key = self.selected_key() or ""
        text: str | None = None
        if key == "spec":
            text = self._cockpit.spec_id
        elif key.startswith("ac:"):
            text = key[len("ac:"):]
        elif key.startswith("task:"):
            task = self._selected_task()
            text = task.branch if task is not None else None
        elif key.startswith("pr:"):
            pr = self._selected_pr()
            text = pr.url if pr is not None else None
        elif key.startswith("thread:"):
            text = key[len("thread:"):]
        if text:
            self.copy_to_clipboard(text)
            self._announce(f"Copied {text}")
        else:
            self._announce("Nothing to copy here.")

    def _do_open_entity(self) -> bool:
        # enter opens the linked entity for EVERY cockpit row: the spec opens its
        # full body, a PR opens in the browser, and any other row (task, thread,
        # criterion) opens its detail in a focused in-process screen.
        key = self.selected_key()
        if key is None:
            return False
        if key == "spec" and self._spec_store is not None and self._cockpit.spec_id:
            spec = self._spec_store.find_by_id(self._cockpit.spec_id)
            if spec is not None:
                self.push_screen(EntityScreen(self._cockpit.spec_id, spec.body))
                return True
        pr = self._selected_pr()
        if pr is not None:
            webbrowser.open(pr.url)
            self._announce(f"Opened {pr.url}")
            return True
        row = next((r for r in self.list_rows() if r.key == key), None)
        if row is not None:
            self.push_screen(EntityScreen(key, row.detail))
            return True
        return False


class FollowedItemView(WorkItemCockpitView):
    """`mship view item --follow`: re-resolve the focused WorkItem's cockpit on a
    timer so the pane re-scopes on focus change AND re-renders on data change (ac2)."""
    def __init__(self, provider, interval: float = 2.0, spec_store=None, **kw) -> None:
        super().__init__(cockpit=None, spec_store=spec_store, **kw)
        self._provider = provider
        self._follow_interval = interval

    async def on_mount(self) -> None:
        await self._follow_tick()
        self.set_interval(self._follow_interval, self._follow_tick)

    async def _follow_tick(self) -> None:
        cockpit = self._provider()
        self._cockpit = cockpit
        self._spec_status = cockpit.spec_status if cockpit is not None else None
        await self.reload_rows()


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
    def _run_item_cockpit(item_id: str) -> None:
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

        from mship.core.spec_store import SPECS_DIRNAME, SpecStore
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)
        WorkItemCockpitView(cockpit, spec_store=store).run()

    def _run_followed_item(interval: float) -> None:
        from mship.cli.output import Output
        from mship.cli.view._follow import follow_hint, read_focused_id
        from mship.core.view.workitem_cockpit import render_text
        from mship.core.spec_store import SPECS_DIRNAME, SpecStore

        container = get_container()

        def _provider():
            item_id = read_focused_id(container)
            if item_id is None:
                return None
            return _resolve_cockpit(container, item_id)

        if not Output().is_tty:
            cockpit = _provider()
            typer.echo(follow_hint() if cockpit is None else render_text(cockpit))
            return

        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)
        FollowedItemView(provider=_provider, interval=interval, spec_store=store).run()

    @app.command(name="item")
    def item(
        item_id: Optional[str] = typer.Argument(None, help="WorkItem id to open (e.g. wi-...)"),
        follow: bool = typer.Option(False, "--follow", help="Track the workspace CURRENT FOCUS (cockpit-v2)."),
        interval: float = typer.Option(2.0, "--interval"),
    ):
        """Single-WorkItem cockpit: spec (status + phase), acceptance criteria with
        evidence, tasks + worktrees, and linked PRs + threads."""
        if follow:
            _run_followed_item(interval)
            return
        if item_id is None:
            typer.echo("Error: provide a WorkItem id, or --follow.", err=True)
            raise typer.Exit(code=1)
        _run_item_cockpit(item_id)

    @app.command(name="workitem", hidden=True)
    def workitem(
        item_id: str = typer.Argument(..., help="Deprecated alias for `view item`."),
    ):
        """Deprecated: use `mship view item <id>`."""
        typer.echo(
            "Note: `mship view workitem` is deprecated; use `mship view item`.",
            err=True,
        )
        _run_item_cockpit(item_id)
