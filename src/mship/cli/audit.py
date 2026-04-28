from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def audit(
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names"),
        json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    ):
        """Report git-state drift across workspace repos."""
        from mship.core.repo_state import audit_repos

        container = get_container()
        output = Output()
        config = container.config()
        shell = container.shell()

        names: list[str] | None = None
        if repos:
            names = [n.strip() for n in repos.split(",") if n.strip()]

        try:
            from mship.core.audit_gate import collect_known_worktree_paths
            known = collect_known_worktree_paths(container.state_manager())
        except Exception:
            known = frozenset()

        try:
            report = audit_repos(config, shell, names=names, known_worktree_paths=known)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        # Surface passive worktree issues (passive_drift, passive_fetch_failed,
        # passive_dirty_worktree). Iterates state.tasks; for each (task, repo)
        # in task.passive_repos, audits the worktree and merges issues into
        # the matching RepoAudit entry in `report`.
        try:
            from mship.core.repo_state import (
                audit_passive_worktrees, RepoAudit, AuditReport, Issue,
            )
            state = container.state_manager().load()

            # Group passive worktrees by task. For each task, run a separate
            # audit so messages can include the task slug for disambiguation.
            passive_per_task: list[tuple[str, dict, dict, dict]] = []
            for task_slug, task in state.tasks.items():
                if not task.passive_repos:
                    continue
                paths: dict = {}
                refs: dict = {}
                canonicals: dict = {}
                for repo_name in task.passive_repos:
                    if repo_name not in config.repos:
                        continue
                    rc = config.repos[repo_name]
                    ref = getattr(rc, "expected_branch", None) or getattr(rc, "base_branch", None)
                    if ref is None:
                        continue
                    wt = task.worktrees.get(repo_name)
                    if wt is None:
                        continue
                    paths[repo_name] = wt
                    refs[repo_name] = ref
                    canonicals[repo_name] = rc.path
                if paths:
                    passive_per_task.append((task_slug, paths, refs, canonicals))

            extra_issues_per_repo: dict[str, list] = {}
            for task_slug, paths, refs, canonicals in passive_per_task:
                per_repo = audit_passive_worktrees(paths, refs, canonicals)
                for repo_name, issues in per_repo.items():
                    if not issues:
                        continue
                    # Tag each issue with the task slug for disambiguation
                    tagged = [
                        Issue(i.code, i.severity,
                              f"[task {task_slug}] {i.message}")
                        for i in issues
                    ]
                    extra_issues_per_repo.setdefault(repo_name, []).extend(tagged)

            if extra_issues_per_repo:
                # Rebuild report.repos so RepoAudit instances containing passive
                # repos pick up the merged issues.
                new_repos = []
                seen: set[str] = set()
                for r in report.repos:
                    seen.add(r.name)
                    extra = extra_issues_per_repo.get(r.name, [])
                    if extra:
                        new_repos.append(RepoAudit(
                            name=r.name,
                            path=r.path,
                            current_branch=r.current_branch,
                            issues=tuple(list(r.issues) + extra),
                        ))
                    else:
                        new_repos.append(r)
                # Repos with passive issues but not in the audit_repos report
                # (e.g. excluded by --repos filter). Append fresh entries.
                for repo_name, extra in extra_issues_per_repo.items():
                    if repo_name in seen:
                        continue
                    rc = config.repos.get(repo_name)
                    if rc is None:
                        continue
                    new_repos.append(RepoAudit(
                        name=repo_name,
                        path=rc.path,
                        current_branch=None,
                        issues=tuple(extra),
                    ))
                report = AuditReport(repos=tuple(new_repos))
        except Exception as e:
            # Best-effort: failing to audit passive worktrees should not break
            # the canonical audit output.
            output.warning(f"passive audit unavailable: {e}")

        if json_output:
            import json as _json
            print(_json.dumps(report.to_json(workspace=config.workspace), indent=2))
            raise typer.Exit(code=1 if report.has_errors else 0)

        output.print(f"[bold]workspace:[/bold] {config.workspace}")
        output.print("")
        err_count = 0
        warn_count = 0
        info_count = 0
        for r in report.repos:
            branch_suffix = f" ({r.current_branch})" if r.current_branch else ""
            output.print(f"[bold]{r.name}[/bold]{branch_suffix}:")
            if not r.issues:
                output.print("  [green]✓[/green] clean")
            else:
                for i in r.issues:
                    if i.severity == "error":
                        err_count += 1
                        output.print(f"  [red]✗[/red] {i.code}: {i.message}")
                    elif i.severity == "warn":
                        warn_count += 1
                        output.print(f"  [yellow]⚠[/yellow] {i.code}: {i.message}")
                    else:
                        info_count += 1
                        output.print(f"  [blue]ⓘ[/blue] {i.code}: {i.message}")
            output.print("")
        output.print(f"{err_count} error(s), {warn_count} warn(s), {info_count} info across {len(report.repos)} repos")
        raise typer.Exit(code=1 if report.has_errors else 0)
