from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output
from mship.core.init import WorkspaceInitializer, DetectedRepo


def _unique_git_roots(config) -> list[Path]:
    """Return deduplicated effective git roots for all repos in config."""
    roots: set[Path] = set()
    for name, repo in config.repos.items():
        if repo.git_root is not None and repo.git_root in config.repos:
            roots.add(Path(config.repos[repo.git_root].path).resolve())
        else:
            roots.add(Path(repo.path).resolve())
    return sorted(roots)


def register(app: typer.Typer, get_container):
    @app.command()
    def init(
        path: Optional[str] = typer.Argument(None, help="Workspace directory (defaults to current directory)"),
        name: Optional[str] = typer.Option(None, "--name", help="Workspace name"),
        repo: Optional[list[str]] = typer.Option(None, "--repo", help="Repo in format path:type[:dep1,dep2]"),
        detect: bool = typer.Option(False, "--detect", help="Auto-detect repos in current directory"),
        env_runner: Optional[str] = typer.Option(None, "--env-runner", help="Secret management command prefix"),
        scaffold_taskfiles: bool = typer.Option(False, "--scaffold-taskfiles", help="Create starter Taskfile.yml for repos without one"),
        force: bool = typer.Option(False, "--force", help="Overwrite existing mothership.yaml"),
        install_hooks_only: bool = typer.Option(
            False, "--install-hooks",
            help="Only install git hooks on every known git root (skip the rest of init).",
        ),
    ):
        """Initialize a new mothership workspace."""
        output = Output()
        cwd = Path(path).resolve() if path else Path.cwd()
        config_path = cwd / "mothership.yaml"
        initializer = WorkspaceInitializer()

        # --install-hooks: short-circuit — just install hooks on each known git root
        if install_hooks_only:
            from mship.core.hooks import install_hook, InstallOutcome
            container = get_container()
            config = container.config()
            installed_results: list[tuple[Path, dict[str, InstallOutcome]]] = []
            failed: list[tuple[Path, str]] = []
            for root in _unique_git_roots(config):
                try:
                    outcomes = install_hook(root)
                    installed_results.append((root, outcomes))
                except Exception as e:
                    failed.append((root, str(e)))
            for root, outcomes in installed_results:
                hooks_dir = root / ".git" / "hooks"
                for hook_name in ("pre-commit", "post-commit", "post-checkout"):
                    outcome = outcomes.get(hook_name)
                    if outcome is None:
                        continue
                    line = f"{hook_name} @ {hooks_dir}/: {outcome.value}"
                    if outcome in (InstallOutcome.installed, InstallOutcome.refreshed):
                        output.success(line)
                    elif outcome is InstallOutcome.skipped_corrupt:
                        output.warning(line)
                    else:
                        output.print(line)
            for r, err in failed:
                output.error(f"hook install failed: {r}: {err}")
            raise typer.Exit(code=1 if failed else 0)

        # Check for existing config
        if config_path.exists() and not force:
            output.error("mothership.yaml already exists. Use --force to overwrite.")
            raise typer.Exit(code=1)

        # Interactive mode
        if output.is_tty and name is None and not repo and not detect:
            _run_interactive(initializer, output, cwd, config_path, env_runner, force)
            return

        # Non-interactive mode
        if name is None:
            output.error("--name is required in non-interactive mode")
            raise typer.Exit(code=1)

        if not repo and not detect:
            output.error("Provide --repo flags or --detect in non-interactive mode")
            raise typer.Exit(code=1)

        repos_data: list[dict] = []

        # Parse --repo flags
        if repo:
            for r in repo:
                parsed = _parse_repo_flag(r, cwd)
                repos_data.append(parsed)

        # Auto-detect
        if detect:
            detected = initializer.detect_repos(cwd)
            existing_paths = {Path(rd["path"]).resolve() for rd in repos_data}
            for d in detected:
                if d.path.resolve() not in existing_paths:
                    repo_name = d.path.name if d.path != cwd else cwd.name
                    repos_data.append({
                        "name": repo_name,
                        "path": d.path,
                        "type": "service",
                        "depends_on": [],
                    })

        try:
            config = initializer.generate_config(name, repos_data, env_runner)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        initializer.write_config(config_path, config)

        # Scaffold Taskfiles
        created_taskfiles: list[str] = []
        if scaffold_taskfiles:
            for rd in repos_data:
                repo_path = Path(rd["path"])
                if not (repo_path / "Taskfile.yml").exists():
                    initializer.write_taskfile(repo_path)
                    created_taskfiles.append(str(repo_path))

        # Install pre-commit hooks on each effective git root
        from mship.core.hooks import install_hook
        for root in _unique_git_roots(config):
            try:
                install_hook(root)
            except Exception as e:
                output.print(f"[yellow]warning: could not install hook at {root}: {e}[/yellow]")

        if output.is_tty:
            output.success(f"Created: {config_path}")
            for tf in created_taskfiles:
                output.success(f"Created: {tf}/Taskfile.yml")
            output.print("\nRun `mship status` to verify your workspace.")
        else:
            output.json({
                "config": str(config_path),
                "taskfiles_created": created_taskfiles,
            })


def _parse_repo_flag(value: str, cwd: Path) -> dict:
    """Parse 'path:type[:dep1,dep2]' format."""
    parts = value.split(":")
    if len(parts) < 2:
        raise typer.Exit(code=1)

    path_str = parts[0]
    repo_type = parts[1]
    depends_on = parts[2].split(",") if len(parts) > 2 else []

    path = (cwd / path_str).resolve()
    repo_name = Path(path_str).name
    if repo_name == ".":
        repo_name = path.name

    return {
        "name": repo_name,
        "path": path,
        "type": repo_type,
        "depends_on": depends_on,
    }


def _run_interactive(
    initializer: WorkspaceInitializer,
    output: Output,
    cwd: Path,
    config_path: Path,
    env_runner: str | None,
    force: bool,
):
    """Run the interactive wizard using InquirerPy."""
    from InquirerPy import inquirer

    output.print("[bold]Welcome to Mothership![/bold] Let's set up your workspace.\n")

    # 1. Workspace name
    default_name = cwd.name
    workspace_name = inquirer.text(
        message="Workspace name:",
        default=default_name,
    ).execute()

    # 2. Detect repos
    output.print("\nScanning for repositories...")
    detected = initializer.detect_repos(cwd)

    if detected:
        choices = []
        for d in detected:
            rel = d.path.relative_to(cwd) if d.path != cwd else Path(".")
            marker_str = ", ".join(d.markers)
            label = f"./{rel} (has {marker_str})"
            choices.append({"name": label, "value": d, "enabled": True})

        selected = inquirer.checkbox(
            message="Select repos to include:",
            choices=choices,
        ).execute()
    else:
        output.print("No repos detected automatically.")
        selected = []

    # 3. Manual add
    while True:
        extra = inquirer.text(
            message="Add another repo path? (enter path or leave blank to skip):",
            default="",
        ).execute()
        if not extra:
            break
        extra_path = (cwd / extra).resolve()
        if extra_path.is_dir():
            selected.append(DetectedRepo(path=extra_path, markers=[]))
        else:
            output.warning(f"Path does not exist: {extra_path}")

    if not selected:
        output.error("No repos selected. Aborting.")
        raise typer.Exit(code=1)

    # 4. Repo types
    repos_data: list[dict] = []
    for det in selected:
        repo_name = det.path.name if det.path != cwd else cwd.name
        repo_type = inquirer.select(
            message=f'What type is "{repo_name}"?',
            choices=["library", "service"],
            default="service",
        ).execute()
        repos_data.append({
            "name": repo_name,
            "path": det.path,
            "type": repo_type,
            "depends_on": [],
        })

    # 5. Dependencies
    repo_names = [r["name"] for r in repos_data]
    for rd in repos_data:
        other_repos = [n for n in repo_names if n != rd["name"]]
        if other_repos:
            deps = inquirer.checkbox(
                message=f'What does "{rd["name"]}" depend on?',
                choices=other_repos,
            ).execute()
            rd["depends_on"] = deps

    # 6. Taskfile scaffolding
    created_taskfiles: list[str] = []
    for rd in repos_data:
        repo_path = Path(rd["path"])
        if not (repo_path / "Taskfile.yml").exists():
            scaffold = inquirer.confirm(
                message=f'"{rd["name"]}" has no Taskfile.yml. Create a starter?',
                default=True,
            ).execute()
            if scaffold:
                initializer.write_taskfile(repo_path)
                created_taskfiles.append(rd["name"])

    # 7. Env runner
    if env_runner is None:
        env_choice = inquirer.select(
            message="Secret management (env_runner)?",
            choices=[
                {"name": "None", "value": None},
                {"name": "dotenvx run --", "value": "dotenvx run --"},
                {"name": "doppler run --", "value": "doppler run --"},
                {"name": "op run --", "value": "op run --"},
                {"name": "Custom...", "value": "__custom__"},
            ],
            default=None,
        ).execute()
        if env_choice == "__custom__":
            env_runner = inquirer.text(message="Enter custom env_runner:").execute()
        else:
            env_runner = env_choice

    # 8. Generate and write
    try:
        config = initializer.generate_config(workspace_name, repos_data, env_runner)
    except ValueError as e:
        output.error(str(e))
        raise typer.Exit(code=1)

    initializer.write_config(config_path, config)

    # Install pre-commit hooks on each effective git root
    from mship.core.hooks import install_hook
    for root in _unique_git_roots(config):
        try:
            install_hook(root)
        except Exception as e:
            output.print(f"[yellow]warning: could not install hook at {root}: {e}[/yellow]")

    output.print("")
    output.success(f"Created: {config_path}")
    for name in created_taskfiles:
        output.success(f"Created: {name}/Taskfile.yml")
    output.print("\nRun `mship status` to verify your workspace.")
