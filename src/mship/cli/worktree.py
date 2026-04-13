from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def spawn(
        description: str,
        repos: Optional[str] = typer.Option(None, help="Comma-separated repo names"),
        skip_setup: bool = typer.Option(False, "--skip-setup", help="Skip running `task setup` in new worktrees"),
    ):
        """Create coordinated worktrees across repos for a new task."""
        container = get_container()
        output = Output()
        wt_mgr = container.worktree_manager()

        repo_list = repos.split(",") if repos else None

        if output.is_tty and not skip_setup:
            output.print("[dim]Running setup in each worktree (use --skip-setup to skip)...[/dim]")

        result = wt_mgr.spawn(description, repos=repo_list, skip_setup=skip_setup)
        task = result.task

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
    def abort(
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    ):
        """Discard worktrees and abandon the current task."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task to abort. Run `mship spawn` to start one.")
            raise typer.Exit(code=1)

        task_slug = state.current_task

        if not yes and output.is_tty:
            from InquirerPy import inquirer

            confirm = inquirer.confirm(
                message=f"Abort task '{task_slug}'? This will remove all worktrees.",
                default=False,
            ).execute()
            if not confirm:
                output.print("Aborted")
                raise typer.Exit(code=0)

        wt_mgr = container.worktree_manager()
        wt_mgr.abort(task_slug)
        output.success(f"Aborted task: {task_slug}")

    @app.command()
    def finish(
        handoff: bool = typer.Option(False, "--handoff", help="Generate CI handoff manifest"),
        base: Optional[str] = typer.Option(None, "--base", help="Global override of PR base branch for all repos"),
        base_map: Optional[str] = typer.Option(None, "--base-map", help="Per-repo PR base overrides, e.g. 'cli=main,api=release/x'"),
    ):
        """Create PRs across repos in dependency order."""
        from pathlib import Path

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task to finish. Run `mship spawn` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        graph = container.graph()
        config = container.config()
        ordered = graph.topo_sort(task.affected_repos)

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
        for repo_name, eff_base in effective_bases.items():
            if eff_base is None:
                continue
            if repo_name in task.pr_urls:
                continue  # skip repos already done
            repo_path = config.repos[repo_name].path
            if repo_name in task.worktrees:
                from pathlib import Path as _P
                wt = _P(task.worktrees[repo_name])
                if wt.exists():
                    repo_path = wt
            if not pr_mgr.verify_base_exists(repo_path, eff_base):
                missing.append((repo_name, eff_base))

        if missing:
            output.error("Base branch not found on remote:")
            for repo_name, eff_base in missing:
                output.error(f"  {repo_name}: {eff_base}")
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

            # Create PR
            try:
                pr_url = pr_mgr.create_pr(
                    repo_path=repo_path,
                    branch=task.branch,
                    title=task.description,
                    body=task.description,
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

        if output.is_tty:
            output.print("")
            output.success(f"Created {len(pr_list)} PR(s) for task: {task.slug}")
            if len(pr_list) > 1:
                output.print("Merge in dependency order as shown in each PR description.")
        else:
            output.json({
                "task": task.slug,
                "prs": pr_list,
            })
