from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def spawn(
        description: str,
        repos: Optional[str] = typer.Option(None, help="Comma-separated repo names"),
        skip_setup: bool = typer.Option(False, "--skip-setup", help="Skip running `task setup` in new worktrees"),
        force_audit: bool = typer.Option(False, "--force-audit", help="Bypass audit gate for this spawn"),
    ):
        """Create coordinated worktrees across repos for a new task."""
        from mship.core.audit_gate import run_audit_gate, AuditGateBlocked
        from mship.core.repo_state import audit_repos

        container = get_container()
        output = Output()
        wt_mgr = container.worktree_manager()
        config = container.config()
        shell = container.shell()

        repo_list = repos.split(",") if repos else None
        audit_names = repo_list if repo_list else list(config.repos.keys())

        from mship.core.audit_gate import collect_known_worktree_paths
        try:
            known = collect_known_worktree_paths(container.state_manager())
        except Exception:
            known = frozenset()
        report = audit_repos(config, shell, names=audit_names, known_worktree_paths=known)

        pending_bypass: list[list[str]] = []

        def _log_bypass(codes: list[str]) -> None:
            pending_bypass.append(codes)

        try:
            run_audit_gate(
                report,
                block=config.audit.block_spawn,
                force=force_audit,
                command_name="spawn",
                on_bypass=_log_bypass,
            )
        except AuditGateBlocked as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if report.has_errors and not config.audit.block_spawn and not force_audit:
            error_summary = ", ".join(
                f"{r.name}:{i.code}"
                for r in report.repos
                for i in r.issues
                if i.severity == "error"
            )
            output.print(f"[yellow]warning:[/yellow] spawn proceeding despite audit errors ({error_summary})")

        # --- git_root validation: every repo that has a git_root must have
        #     that root included in the target set, otherwise worktree isolation
        #     will silently operate on the main checkout instead.
        target_repos = repo_list if repo_list else list(config.repos.keys())
        gitroot_violations: list[tuple[str, str]] = []
        for r in target_repos:
            root = config.repos[r].git_root
            if root is not None and root not in target_repos:
                gitroot_violations.append((r, root))
        if gitroot_violations:
            output.error("Cannot spawn: some repos share a git_root with repos not in this task.")
            output.error("Worktree isolation will not work because they share one git checkout.")
            for r, root in gitroot_violations:
                output.error(f"  {r} shares git_root with {root!r} (missing from --repos)")
            missing = sorted({root for _, root in gitroot_violations})
            if repo_list:
                suggestion = ",".join(sorted(set(repo_list) | set(missing)))
                output.error(f"")
                output.error(f"Add missing repos: --repos {suggestion}")
            else:
                output.error(f"")
                output.error(f"Add missing repos to the spawn: --repos {','.join(sorted(missing))}")
            raise typer.Exit(code=1)

        if output.is_tty and not skip_setup:
            output.print("[dim]Running setup in each worktree (use --skip-setup to skip)...[/dim]")

        result = wt_mgr.spawn(description, repos=repo_list, skip_setup=skip_setup)
        task = result.task

        if pending_bypass:
            log_mgr = container.log_manager()
            for codes in pending_bypass:
                log_mgr.append(task.slug, f"BYPASSED AUDIT: spawn — {', '.join(codes)}")

        if output.is_tty:
            output.success(f"Spawned task: {task.slug}")
            output.print(f"  Branch: {task.branch}")
            output.print(f"  Phase: {task.phase}")
            output.print(f"  Repos: {', '.join(task.affected_repos)}")
            for repo, path in task.worktrees.items():
                output.print(f"  {repo}: {path}")
            for warning in result.setup_warnings:
                output.warning(warning)
        else:
            data = task.model_dump(mode="json")
            data["setup_warnings"] = result.setup_warnings
            output.json(data)

    @app.command()
    def worktrees():
        """List active worktrees grouped by task."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if not state.tasks:
            output.print("No active worktrees")
            return

        if output.is_tty:
            for slug, task in state.tasks.items():
                active = " (active)" if slug == state.current_task else ""
                output.print(f"[bold]{slug}[/bold]{active} [{task.phase}]")
                output.print(f"  Branch: {task.branch}")
                for repo, path in task.worktrees.items():
                    output.print(f"  {repo}: {path}")
        else:
            data = {
                slug: task.model_dump(mode="json")
                for slug, task in state.tasks.items()
            }
            output.json({"current_task": state.current_task, "tasks": data})

    @app.command()
    def close(
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
        force: bool = typer.Option(False, "--force", "-f", help="Bypass ALL safety checks (destructive)"),
        abandon: bool = typer.Option(False, "--abandon", help="Close without finishing (discard PR flow)"),
        skip_pr_check: bool = typer.Option(False, "--skip-pr-check", help="Do not call gh; close regardless of PR state"),
    ):
        """Close the current task: check PR state, tear down worktrees, clear state."""
        from pathlib import Path

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task to close. Run `mship spawn` to start one.")
            raise typer.Exit(code=1)

        task_slug = state.current_task
        task = state.tasks[task_slug]
        pr_mgr = container.pr_manager()
        config = container.config()
        log_mgr = container.log_manager()

        # --- Finish-required check ---
        if task.finished_at is None and not abandon and not force:
            output.error(
                "Cannot close: task hasn't been finished.\n"
                "  Run `mship finish` to create PRs, or `mship close --abandon` to discard without PRs."
            )
            raise typer.Exit(code=1)

        # --- Recovery-path check ---
        had_unrecoverable = False
        if not force:
            from mship.core.base_resolver import resolve_base
            unrecoverable: list[tuple[str, int, str, str]] = []  # (repo, commits, branch, base)
            for repo_name in task.affected_repos:
                wt = task.worktrees.get(repo_name)
                if wt is None:
                    continue
                wt_path = Path(wt)
                if not wt_path.exists():
                    continue
                eff_base = resolve_base(
                    repo_name, config.repos[repo_name],
                    cli_base=None, base_map={}, known_repos=config.repos.keys(),
                )
                if eff_base is None:
                    eff_base = "main"  # fall back to main when no base_branch configured
                commits = pr_mgr.count_commits_ahead(wt_path, eff_base, task.branch)
                if commits == 0:
                    continue
                # Recovery checks
                merged = pr_mgr.check_merged_into_base(wt_path, task.branch, eff_base)
                has_pr = repo_name in task.pr_urls
                pushed = pr_mgr.check_pushed_to_origin(wt_path, task.branch)
                if merged or has_pr or pushed:
                    continue
                unrecoverable.append((repo_name, commits, task.branch, eff_base))

            if unrecoverable:
                had_unrecoverable = True
                output.error("Cannot close: unrecoverable commits in these repos:")
                for repo_name, commits, branch, eff_base in unrecoverable:
                    output.error(
                        f"  {repo_name}: {branch} ({commits} commits, "
                        f"not merged to {eff_base}, not pushed, no PR)"
                    )
                output.error("")
                output.error("These will be permanently lost. Options:")
                output.error("  - `mship finish` to create PRs")
                output.error("  - push from each worktree to save work")
                output.error("  - `mship close --force` to delete anyway (destructive)")
                raise typer.Exit(code=1)

        # Determine the log message based on PR state.
        pr_states: list[str] = []  # parallel to task.pr_urls values
        if task.pr_urls and not skip_pr_check:
            import shutil
            if shutil.which("gh") is None and not force:
                output.error(
                    "gh CLI needed to check PR state. Install gh, or pass --skip-pr-check."
                )
                raise typer.Exit(code=1)
            for url in task.pr_urls.values():
                pr_states.append(pr_mgr.check_pr_state(url))

        # Route on PR states
        open_count = sum(1 for s in pr_states if s == "open")
        merged_count = sum(1 for s in pr_states if s == "merged")
        closed_count = sum(1 for s in pr_states if s == "closed")

        if task.pr_urls and skip_pr_check:
            log_msg = "closed: pr state unchecked"
        elif not task.pr_urls:
            if task.finished_at is not None:
                log_msg = "closed: no PRs (pushed via --push-only)"
            elif abandon:
                log_msg = "closed: cancelled before finish (abandoned)"
            else:
                log_msg = "closed: cancelled before finish"
        elif open_count and not force:
            output.error(
                f"Task '{task_slug}' has {open_count} open PR(s). Merge or close them first, "
                f"or pass --force to override."
            )
            raise typer.Exit(code=1)
        elif open_count and force:
            log_msg = f"closed: forced with open PRs ({open_count} open)"
        elif merged_count and not closed_count:
            log_msg = f"closed: completed ({merged_count} PRs merged)"
        elif closed_count and not merged_count:
            log_msg = "closed: cancelled on GitHub"
        elif merged_count and closed_count:
            log_msg = f"closed: mixed ({merged_count} merged, {closed_count} closed)"
        else:
            log_msg = "closed: pr state unknown"

        # Append (forced) marker when --force bypassed finish/recovery gates.
        if force and (task.finished_at is None or had_unrecoverable):
            if "(forced)" not in log_msg:
                log_msg = log_msg + " (forced)"

        if not yes and output.is_tty:
            from InquirerPy import inquirer
            confirm = inquirer.confirm(
                message=f"Close task '{task_slug}'? This will remove all worktrees.",
                default=False,
            ).execute()
            if not confirm:
                output.print("Cancelled")
                raise typer.Exit(code=0)

        wt_mgr = container.worktree_manager()
        wt_mgr.abort(task_slug)  # core method retains the name; only CLI verb changed
        log_mgr.append(task_slug, log_msg)
        output.success(f"{log_msg.capitalize()}: {task_slug}")

    @app.command()
    def finish(
        handoff: bool = typer.Option(False, "--handoff", help="Generate CI handoff manifest"),
        base: Optional[str] = typer.Option(None, "--base", help="Global override of PR base branch for all repos"),
        base_map: Optional[str] = typer.Option(None, "--base-map", help="Per-repo PR base overrides, e.g. 'cli=main,api=release/x'"),
        force_audit: bool = typer.Option(False, "--force-audit", help="Bypass audit gate for this finish"),
        push_only: bool = typer.Option(False, "--push-only", help="Push branches only; skip gh pr create"),
    ):
        """Create PRs across repos in dependency order."""
        from pathlib import Path

        container = get_container()
        output = Output()

        if push_only and (handoff or base is not None or base_map is not None):
            output.error("--push-only is incompatible with --handoff/--base/--base-map")
            raise typer.Exit(code=1)
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task to finish. Run `mship spawn` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        graph = container.graph()
        config = container.config()
        ordered = graph.topo_sort(task.affected_repos)

        # --- Audit gate ---
        from mship.core.audit_gate import run_audit_gate, AuditGateBlocked
        from mship.core.repo_state import audit_repos

        shell = container.shell()
        from mship.core.audit_gate import collect_known_worktree_paths
        try:
            known = collect_known_worktree_paths(container.state_manager())
        except Exception:
            known = frozenset()
        report = audit_repos(config, shell, names=task.affected_repos, known_worktree_paths=known)

        # finish is what creates the upstream via `git push -u` — so while the
        # task is still unfinished, `no_upstream` on the task's own branch is a
        # false positive that would block every first-time finish. Filter it
        # out of the gate's report; standalone `mship audit` still reports it.
        if task.finished_at is None:
            from mship.core.repo_state import without_no_upstream_on_task_branch
            report = without_no_upstream_on_task_branch(report, task.branch)

        def _log_bypass(codes: list[str]) -> None:
            container.log_manager().append(
                task.slug, f"BYPASSED AUDIT: finish — {', '.join(codes)}"
            )

        try:
            run_audit_gate(
                report,
                block=config.audit.block_finish,
                force=force_audit,
                command_name="finish",
                on_bypass=_log_bypass,
            )
        except AuditGateBlocked as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if report.has_errors and not config.audit.block_finish and not force_audit:
            output.print("[yellow]warning:[/yellow] finish proceeding despite audit errors")

        if handoff:
            from mship.core.handoff import generate_handoff

            state_dir = container.state_dir()
            repo_paths = {name: config.repos[name].path for name in ordered}
            repo_deps = {
                name: [d.repo for d in config.repos[name].depends_on]
                for name in ordered
            }
            path = generate_handoff(
                handoffs_dir=Path(state_dir) / "handoffs",
                task_slug=task.slug,
                branch=task.branch,
                ordered_repos=ordered,
                repo_paths=repo_paths,
                repo_deps=repo_deps,
            )
            if output.is_tty:
                output.success(f"Handoff manifest written to: {path}")
            else:
                output.json({"handoff": str(path), "task": task.slug})
            return

        # PR creation flow
        pr_mgr = container.pr_manager()

        # --push-only: push branches, stamp finished_at, skip gh entirely.
        if push_only:
            push_list: list[dict] = []
            for repo_name in ordered:
                if repo_name in task.pr_urls:
                    continue
                repo_config = config.repos[repo_name]
                repo_path = repo_config.path
                if repo_name in task.worktrees:
                    wt_path = Path(task.worktrees[repo_name])
                    if wt_path.exists():
                        repo_path = wt_path
                try:
                    pr_mgr.push_branch(repo_path, task.branch)
                except RuntimeError as e:
                    output.error(f"{repo_name}: {e}")
                    raise typer.Exit(code=1)
                if output.is_tty:
                    output.print(f"  {repo_name}: {task.branch} pushed")
                push_list.append({"repo": repo_name, "branch": task.branch, "pushed": True})

            from datetime import datetime as _dt, timezone as _tz
            if task.finished_at is None:
                task.finished_at = _dt.now(_tz.utc)
                state_mgr.save(state)

            if output.is_tty:
                output.print("[green]Branch pushed.[/green] After merge/review, run `mship close` to clean up.")
            else:
                output.json({"task": task.slug, "pushed": [p["repo"] for p in push_list], "finished_at": task.finished_at.isoformat()})
                output.print("Branch pushed. After merge/review, run `mship close` to clean up.")
            return

        try:
            pr_mgr.check_gh_available()
        except RuntimeError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        # --- Resolve + verify PR base branches up front ---
        from mship.core.base_resolver import (
            parse_base_map,
            resolve_base,
            InvalidBaseMapError,
            UnknownRepoInBaseMapError,
        )

        try:
            parsed_map = parse_base_map(base_map or "")
        except InvalidBaseMapError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        try:
            effective_bases = {
                repo_name: resolve_base(
                    repo_name,
                    config.repos[repo_name],
                    cli_base=base,
                    base_map=parsed_map,
                    known_repos=config.repos.keys(),
                )
                for repo_name in ordered
            }
        except UnknownRepoInBaseMapError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        missing: list[tuple[str, str]] = []
        empty_branches: list[tuple[str, str, str]] = []
        for repo_name, eff_base in effective_bases.items():
            if repo_name in task.pr_urls:
                continue  # skip repos already done
            repo_path = config.repos[repo_name].path
            if repo_name in task.worktrees:
                wt = Path(task.worktrees[repo_name])
                if wt.exists():
                    repo_path = wt
            if eff_base is not None:
                if not pr_mgr.verify_base_exists(repo_path, eff_base):
                    missing.append((repo_name, eff_base))
                    continue
                if pr_mgr.count_commits_ahead(repo_path, eff_base, task.branch) == 0:
                    empty_branches.append((repo_name, task.branch, eff_base))

        if missing:
            output.error("Base branch not found on remote:")
            for repo_name, eff_base in missing:
                output.error(f"  {repo_name}: {eff_base}")
            raise typer.Exit(code=1)

        if empty_branches:
            output.error("No commits to push — nothing to PR:")
            for repo_name, branch, eff_base in empty_branches:
                output.error(f"  {repo_name}: {branch} has no commits past {eff_base}")
            output.error("Commit your changes in each worktree, or run `mship abort --yes`.")
            raise typer.Exit(code=1)

        pr_list: list[dict] = []

        for i, repo_name in enumerate(ordered, 1):
            # Skip if PR already created (idempotent re-run)
            if repo_name in task.pr_urls:
                if output.is_tty:
                    output.print(f"  {repo_name}: already has PR {task.pr_urls[repo_name]}")
                pr_list.append({
                    "repo": repo_name,
                    "url": task.pr_urls[repo_name],
                    "order": i,
                    "base": effective_bases.get(repo_name),
                })
                continue

            repo_config = config.repos[repo_name]

            # Use worktree path if available
            repo_path = repo_config.path
            if repo_name in task.worktrees:
                wt_path = Path(task.worktrees[repo_name])
                if wt_path.exists():
                    repo_path = wt_path

            # Push branch
            try:
                pr_mgr.push_branch(repo_path, task.branch)
            except RuntimeError as e:
                output.error(f"{repo_name}: {e}")
                raise typer.Exit(code=1)

            # Build the PR body — appends `Closes #N` for any GitHub issue
            # references in the task description, log entries, or commit subjects.
            from mship.core.issue_refs import append_closes_footer, extract_issue_refs
            texts: list[str] = [task.description]
            try:
                entries = container.log_manager().read(task.slug)
                for e in entries:
                    if e.message:
                        texts.append(e.message)
                    if e.action:
                        texts.append(e.action)
                    if e.open_question:
                        texts.append(e.open_question)
            except Exception:
                pass
            try:
                eff_base = effective_bases[repo_name] or "HEAD"
                import shlex as _shlex
                subjects_res = shell.run(
                    f"git log --format=%s origin/{_shlex.quote(eff_base)}..{_shlex.quote(task.branch)}",
                    cwd=repo_path,
                )
                if subjects_res.returncode == 0:
                    for line in subjects_res.stdout.splitlines():
                        if line.strip():
                            texts.append(line)
            except Exception:
                pass
            pr_body = append_closes_footer(task.description, extract_issue_refs(texts))

            # Create PR
            try:
                pr_url = pr_mgr.create_pr(
                    repo_path=repo_path,
                    branch=task.branch,
                    title=task.description,
                    body=pr_body,
                    base=effective_bases[repo_name],
                )
            except RuntimeError as e:
                output.error(f"{repo_name}: {e}")
                raise typer.Exit(code=1)

            # Store in state (crash-safe: save after each PR)
            task.pr_urls[repo_name] = pr_url
            state_mgr.save(state)

            pr_list.append({"repo": repo_name, "url": pr_url, "order": i})

            base_label = effective_bases[repo_name] or "(default)"
            if output.is_tty:
                output.print(f"  {repo_name}: {task.branch} → {base_label}  ✓ {pr_url}")
            pr_list[-1]["base"] = effective_bases[repo_name]

        # Update PRs with coordination blocks (multi-repo only)
        if len(pr_list) > 1:
            for pr_info in pr_list:
                block = pr_mgr.build_coordination_block(
                    task.slug, pr_list, current_repo=pr_info["repo"]
                )
                if block:
                    existing_body = pr_mgr.get_pr_body(pr_info["url"])
                    new_body = existing_body + block
                    pr_mgr.update_pr_body(pr_info["url"], new_body)

        # Log PR URLs
        log_mgr = container.log_manager()
        for pr_info in pr_list:
            log_mgr.append(
                state.current_task,
                f"PR created for {pr_info['repo']}: {pr_info['url']}",
            )

        # Stamp finished_at on successful PR creation.
        from datetime import datetime as _dt, timezone as _tz
        if task.finished_at is None:
            task.finished_at = _dt.now(_tz.utc)
            state_mgr.save(state)

        if output.is_tty:
            output.print("")
            output.success(f"Created {len(pr_list)} PR(s) for task: {task.slug}")
            if len(pr_list) > 1:
                output.print("Merge in dependency order as shown in each PR description.")
            output.print("[green]Task finished.[/green] After merge, run `mship close` to clean up.")
        else:
            output.json({
                "task": task.slug,
                "prs": pr_list,
                "finished_at": task.finished_at.isoformat(),
            })
            output.print("Task finished. After merge, run `mship close` to clean up.")
