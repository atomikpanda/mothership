"""`mship reconcile` — detect upstream PR drift."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output
from mship.core.reconcile.cache import ReconcileCache
from mship.core.reconcile.detect import UpstreamState
from mship.core.reconcile.fetch import (
    collect_git_snapshots, fetch_pr_snapshots,
)
from mship.core.reconcile.gate import Decision, reconcile_now


_ACTION_HINTS = {
    UpstreamState.merged:       "run `mship close`",
    UpstreamState.closed:       "run `mship close --abandon`",
    UpstreamState.diverged:     "pull and rebase",
    UpstreamState.base_changed: "rebase onto new base",
    UpstreamState.missing:      "—",
    UpstreamState.in_sync:      "—",
}


def _glyph(state: UpstreamState) -> str:
    return "✓" if state in (UpstreamState.in_sync, UpstreamState.missing) else "⚠"


def register(app: typer.Typer, get_container):
    @app.command()
    def reconcile(
        json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
        ignore: Optional[str] = typer.Option(None, "--ignore", help="Persistently ignore drift for this slug"),
        clear_ignores: bool = typer.Option(False, "--clear-ignores", help="Reset the ignore list"),
        refresh: bool = typer.Option(False, "--refresh", help="Skip cache, refetch"),
    ):
        """Detect upstream PR drift across every task in the workspace."""
        output = Output()
        container = get_container()
        state = container.state_manager().load()
        cache = ReconcileCache(container.state_dir())

        if clear_ignores:
            cache.clear_ignores()
            if output.is_tty:
                output.success("Ignore list cleared.")
            else:
                output.json({"cleared": True})
            return

        if ignore is not None:
            if ignore not in state.tasks:
                output.error(f"Unknown task: {ignore!r}.")
                raise typer.Exit(code=1)
            cache.add_ignore(ignore)
            if output.is_tty:
                output.success(f"Ignoring drift for: {ignore}")
            else:
                output.json({"ignored": ignore})
            return

        if refresh:
            payload = cache.read()
            if payload is not None:
                payload.fetched_at = 0.0
                cache.write(payload)

        def _fetcher(branches, worktrees_by_branch):
            return (
                fetch_pr_snapshots(branches),
                collect_git_snapshots(worktrees_by_branch),
            )

        try:
            decisions = reconcile_now(state, cache=cache, fetcher=_fetcher)
        except Exception as e:  # noqa: BLE001 — never fail closed
            output.warning(f"reconcile unavailable: {e}")
            decisions = {}

        _emit(output, decisions, json_out, cache.read_ignores())


def _emit(output: Output, decisions: dict[str, Decision], json_out: bool, ignored: list[str]) -> None:
    if json_out or not output.is_tty:
        output.json({
            "tasks": [
                {
                    "slug": d.slug,
                    "state": d.state.value,
                    "pr_url": d.pr_url,
                    "pr_number": d.pr_number,
                    "base": d.base,
                    "merge_commit": d.merge_commit,
                    "ignored": d.slug in ignored,
                }
                for d in decisions.values()
            ],
            "ignored": ignored,
        })
        return
    if not decisions:
        output.print("No tasks to reconcile.")
        return
    rows: list[list[str]] = []
    for d in decisions.values():
        mark = f"{_glyph(d.state)} {d.state.value}"
        pr = f"#{d.pr_number}" if d.pr_number else "—"
        action = _ACTION_HINTS[d.state]
        if d.slug in ignored:
            action = f"(ignored) {action}"
        rows.append([d.slug, mark, pr, action])
    output.table(
        title="Upstream reconciliation",
        columns=["Task", "State", "PR", "Action"],
        rows=rows,
    )
