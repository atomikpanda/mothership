"""`mship reconcile` — detect upstream PR drift."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output
from mship.core.config import ConfigLoader
from mship.core.reconcile.adopt import AdoptionError, adopt_merged_task
from mship.core.reconcile.cache import ReconcileCache
from mship.core.reconcile.detect import UpstreamState
from mship.core.reconcile.fetch import (
    collect_git_snapshots,
    fetch_pr_snapshots,
)
from mship.core.reconcile.gate import Decision, reconcile_now


_ACTION_HINTS = {
    UpstreamState.merged: "run `mship close`",
    UpstreamState.closed: "run `mship close --abandon`",
    UpstreamState.diverged: "pull and rebase",
    UpstreamState.base_changed: "rebase onto new base",
    UpstreamState.missing: "—",
    UpstreamState.in_sync: "—",
    UpstreamState.dependency_stale: "rebase onto upstream's merge",
}


def _glyph(state: UpstreamState) -> str:
    return "✓" if state in (UpstreamState.in_sync, UpstreamState.missing) else "⚠"


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Inspection")
    def reconcile(
        json_out: bool = typer.Option(
            False, "--json", help="Emit JSON instead of a table"
        ),
        ignore: Optional[str] = typer.Option(
            None, "--ignore", help="Persistently ignore drift for this slug"
        ),
        clear_ignores: bool = typer.Option(
            False, "--clear-ignores", help="Reset the ignore list"
        ),
        refresh: bool = typer.Option(False, "--refresh", help="Skip cache, refetch"),
        adopt_merged: Optional[str] = typer.Option(
            None,
            "--adopt-merged",
            metavar="TASK",
            help="Recover missing metadata for a verified merged task PR.",
        ),
    ):
        """Detect upstream PR drift across every task in the workspace."""
        output = Output()
        container = get_container()
        cache = ReconcileCache(container.state_dir())

        if adopt_merged is not None:
            if ignore is not None or clear_ignores:
                output.error(
                    "--adopt-merged cannot be combined with --ignore or --clear-ignores."
                )
                raise typer.Exit(code=1)
            try:
                config = ConfigLoader.load(container.config_path(), require_paths=False)
                adopted, verified = adopt_merged_task(
                    adopt_merged,
                    state_manager=container.state_manager(),
                    config=config,
                    shell=container.shell(),
                    pr_manager=container.pr_manager(),
                    cache=cache,
                    log=container.log_manager(),
                )
            except AdoptionError as exc:
                output.error(str(exc))
                raise typer.Exit(code=1)
            except Exception as exc:  # Explicit recovery must fail closed.
                output.error(f"Could not adopt merged PR metadata: {exc}")
                raise typer.Exit(code=1)
            result = {
                "task": adopt_merged,
                "adopted": adopted,
                "prs": [
                    {
                        "repo": entry.repo,
                        "url": entry.url,
                        "merge_commit": entry.merge_commit,
                        "merged_at": entry.merged_at.isoformat(),
                    }
                    for entry in verified
                ],
            }
            if json_out or not output.human_mode:
                output.json(result)
            elif adopted:
                output.success(f"Adopted merged PR metadata for: {adopt_merged}")
            else:
                output.success(
                    f"Merged PR metadata is already adopted for: {adopt_merged}"
                )
            if output.human_mode and not json_out:
                for entry in verified:
                    output.print(
                        f"  {entry.repo}: {entry.url} "
                        f"(merge {entry.merge_commit}, merged {entry.merged_at.isoformat()})"
                    )
            return

        state = container.state_manager().load()

        if clear_ignores:
            cache.clear_ignores()
            if output.human_mode:
                output.success("Ignore list cleared.")
            else:
                output.json({"cleared": True})
            return

        if ignore is not None:
            if ignore not in state.tasks:
                output.error(f"Unknown task: {ignore!r}.")
                raise typer.Exit(code=1)
            cache.add_ignore(ignore)
            if output.human_mode:
                output.success(f"Ignoring drift for: {ignore}")
            else:
                output.json({"ignored": ignore})
            return

        config = ConfigLoader.load(container.config_path(), require_paths=False)

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
            decisions = reconcile_now(
                state, cache=cache, fetcher=_fetcher, config=config
            )
        except Exception as e:  # noqa: BLE001 — never fail closed
            output.warning(f"reconcile unavailable: {e}")
            decisions = {}

        _emit(output, decisions, json_out, cache.read_ignores())


def _emit(
    output: Output, decisions: dict[str, Decision], json_out: bool, ignored: list[str]
) -> None:
    if json_out or not output.human_mode:
        output.json(
            {
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
            }
        )
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
