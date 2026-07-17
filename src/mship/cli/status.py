from pathlib import Path
from typing import Iterable, Optional

import typer

from mship.cli.output import Output


def _cwd_inside_any_worktree(cwd: Path, worktree_paths: Iterable[Path]) -> bool:
    """True if `cwd` (resolved) is equal to or nested under any worktree path."""
    try:
        cwd_r = cwd.resolve()
    except OSError:
        return False
    for wt in worktree_paths:
        try:
            wt_r = Path(wt).resolve()
        except OSError:
            continue
        if cwd_r == wt_r or wt_r in cwd_r.parents:
            return True
    return False


def _collect_worktree_paths(state) -> list[Path]:
    paths: list[Path] = []
    for task in state.tasks.values():
        for p in task.worktrees.values():
            paths.append(Path(p))
    return paths


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Inspection")
    def status(
        task: Optional[str] = typer.Option(
            None, "--task", help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var."
        ),
    ):
        """Show workspace summary + resolved task detail (when a task can be
        resolved from cwd / MSHIP_TASK / --task).

        Always returns a single envelope shape (#128) — no bimodal output."""
        from datetime import datetime, timezone
        from mship.util.duration import format_relative
        from mship.core.task_resolver import (
            AmbiguousTaskError, NoActiveTaskError, UnknownTaskError, resolve_task,
        )
        import os
        from pathlib import Path

        container = get_container()
        output = Output()
        config = container.config()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        # --- Resolution: capture both task and source. UnknownTaskError still
        # errors loudly (someone passed --task <unknown> or MSHIP_TASK=<unknown>);
        # NoActive / Ambiguous → resolved_task stays None.
        t = None
        source: str | None = None
        try:
            t, source_enum = resolve_task(
                state,
                cli_task=task,
                env_task=os.environ.get("MSHIP_TASK"),
                cwd=Path.cwd(),
            )
            source = source_enum.value
        except UnknownTaskError as e:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            output.error(f"Unknown task: {e.slug}. Known: {known}.")
            raise typer.Exit(1)
        except (NoActiveTaskError, AmbiguousTaskError):
            pass  # leave t / source as None

        # --- Workspace-level data (always in the envelope).
        active = sorted(
            state.tasks.values(),
            key=lambda tt: (tt.phase_entered_at or tt.created_at),
            reverse=True,
        )
        worktree_paths = _collect_worktree_paths(state)
        any_worktrees = bool(worktree_paths)
        cwd_outside = (
            any_worktrees
            and not _cwd_inside_any_worktree(Path.cwd(), worktree_paths)
        )

        # --- Resolved-task detail (only when a task resolved).
        resolved_payload: dict | None = None
        drift_summary = {"has_errors": False, "error_count": 0}
        last_log: dict | None = None
        if t is not None:
            try:
                from mship.core.repo_state import audit_repos
                from mship.core.audit_gate import collect_known_worktree_paths
                shell = container.shell()
                try:
                    known = collect_known_worktree_paths(state_mgr)
                except Exception:
                    known = frozenset()
                report = audit_repos(
                    config, shell, names=t.affected_repos,
                    known_worktree_paths=known, local_only=True,
                )
                errors = [i for r in report.repos for i in r.issues if i.severity == "error"]
                drift_summary = {"has_errors": bool(errors), "error_count": len(errors)}
            except Exception:
                pass
            try:
                entries = container.log_manager().read(t.slug, last=1)
                if entries:
                    e = entries[-1]
                    first_line = e.message.splitlines()[0] if e.message else ""
                    last_log = {"message": first_line[:60], "timestamp": e.timestamp}
            except Exception:
                last_log = None

            resolved_payload = t.model_dump(mode="json")
            resolved_payload["active_repo"] = t.active_repo
            if t.blocked_reason:
                resolved_payload["phase_display"] = (
                    f"{t.phase} (BLOCKED: {t.blocked_reason})"
                )
            if t.finished_at is not None:
                resolved_payload["close_hint"] = "mship close"
            resolved_payload["drift"] = drift_summary
            resolved_payload["last_log"] = (
                {"message": last_log["message"], "timestamp": last_log["timestamp"].isoformat()}
                if last_log is not None else None
            )

            # --- #104 dependencies block ---
            from mship.core.task_graph import downstream_of, is_ready
            # We deliberately pass an empty dict — `status` is on the hot path and
            # shouldn't trigger network reconcile. The real readiness check happens
            # in `mship finish`. Here, anything not already confirmed merged is
            # treated as "not ready" / "blocked", which is the safe-side answer.
            decisions: dict = {}
            deps_upstream = []
            blocked_by: list[str] = []
            for edge in t.depends_on:
                ready = is_ready(state, edge.upstream_slug, decisions)
                deps_upstream.append({"slug": edge.upstream_slug, "ready": ready})
                if not ready:
                    blocked_by.append(edge.upstream_slug)
            deps_downstream = [{"slug": s} for s in sorted(downstream_of(state, t.slug))]
            resolved_payload["dependencies"] = {
                "upstream": deps_upstream,
                "downstream": deps_downstream,
                "blocked": bool(blocked_by),
                "blocked_by": blocked_by,
            }

        # --- TTY rendering: unchanged. Workspace summary when no task resolves;
        # task-detail block when one does.
        if output.human_mode:
            if t is None:
                if not active:
                    output.print("No active tasks. Run `mship spawn \"description\"`.")
                else:
                    output.print(f"[bold]Active tasks ({len(active)}):[/bold]")
                    for tt in active:
                        phase_rel = (
                            format_relative(tt.phase_entered_at)
                            if tt.phase_entered_at else "—"
                        )
                        output.print(
                            f"  {tt.slug}  "
                            f"phase={tt.phase} (entered {phase_rel})  "
                            f"branch={tt.branch}"
                        )
                    if cwd_outside:
                        output.print(
                            "\n[yellow]⚠ cwd is outside every active task's worktree.[/yellow]"
                        )
                        output.print(
                            "[yellow]  Running tests/git here will not reflect your task's work.[/yellow]"
                        )
                        for tt in active:
                            for repo, path in tt.worktrees.items():
                                output.print(f"  {tt.slug}:{repo} → {path}")
            else:
                output.print(f"[bold]Task:[/bold] {t.slug}")
                if t.finished_at is not None:
                    output.print(
                        f"[yellow]⚠ Finished:[/yellow] {format_relative(t.finished_at)} — run `mship close` after merge"
                    )
                if t.active_repo is not None:
                    output.print(f"[bold]Active repo:[/bold] {t.active_repo}")
                phase_str = t.phase
                if t.phase_entered_at is not None:
                    rel = format_relative(t.phase_entered_at)
                    phase_str = f"{t.phase} (entered {rel})"
                if t.blocked_reason:
                    phase_str = f"{phase_str}  [red]BLOCKED:[/red] {t.blocked_reason}"
                output.print(f"[bold]Phase:[/bold] {phase_str}")
                if t.blocked_at:
                    output.print(f"[bold]Blocked since:[/bold] {t.blocked_at}")
                output.print(f"[bold]Branch:[/bold] {t.branch}")
                output.print(f"[bold]Repos:[/bold] {', '.join(t.affected_repos)}")
                if t.worktrees:
                    output.print("[bold]Worktrees:[/bold]")
                    for repo, path in t.worktrees.items():
                        output.print(f"  {repo}: {path}")
                if t.test_results:
                    output.print("[bold]Tests:[/bold]")
                    for repo, result in t.test_results.items():
                        status_str = (
                            "[green]pass[/green]" if result.status == "pass"
                            else "[red]fail[/red]"
                        )
                        output.print(f"  {repo}: {status_str}")
                if drift_summary["has_errors"]:
                    output.print(
                        f"[bold]Drift:[/bold] [red]{drift_summary['error_count']} error(s)[/red] — run `mship audit`"
                    )
                else:
                    output.print("[bold]Drift:[/bold] [green]clean[/green]")
                if last_log is not None:
                    ts_rel = format_relative(last_log["timestamp"])
                    output.print(f"[bold]Last log:[/bold] \"{last_log['message']}\" ({ts_rel})")
            return

        # --- Config resolution (issue 366 #6): absolute path + how it resolved.
        from mship.core.config import ConfigLoader
        config_path_abs = str(Path(container.config_path()).resolve())
        config_source: str | None = None
        try:
            res = ConfigLoader.discover_with_source(Path.cwd())
            if str(res.path.resolve()) == config_path_abs:
                config_source = res.source
        except Exception:
            config_source = None

        # --- JSON envelope (single shape, always).
        envelope = {
            "workspace": config.workspace,
            "active_tasks": [
                {
                    "slug": tt.slug,
                    "phase": tt.phase,
                    "branch": tt.branch,
                    "phase_entered_at": (
                        tt.phase_entered_at.isoformat()
                        if tt.phase_entered_at else None
                    ),
                }
                for tt in active
            ],
            "resolved_task": resolved_payload,
            "resolution_source": source,
            "config_path": config_path_abs,
            "config_resolution_source": config_source,
        }
        if any_worktrees:
            envelope["cwd_is_outside_worktrees"] = cwd_outside
        output.json(envelope)

    @app.command(rich_help_panel="Inspection")
    def graph():
        """Show repo dependency graph."""
        container = get_container()
        output = Output()
        config = container.config()
        graph_obj = container.graph()
        order = graph_obj.topo_sort()

        if output.human_mode:
            for repo_name in order:
                repo = config.repos[repo_name]
                deps = repo.depends_on
                dep_str = f" -> [{', '.join(d.repo for d in deps)}]" if deps else ""
                type_str = f"({repo.type})"
                output.print(f"  {repo_name} {type_str}{dep_str}")
        else:
            graph_data = {}
            for name, repo in config.repos.items():
                graph_data[name] = {
                    "type": repo.type,
                    "depends_on": [d.repo for d in repo.depends_on],
                    "path": str(repo.path),
                }
            output.json({"repos": graph_data, "order": order})
