"""Hidden mship commands — used by hooks and other internal consumers."""
from pathlib import Path

import typer


def register(app: typer.Typer, get_container):
    @app.command(name="_check-commit", hidden=True)
    def check_commit(toplevel: str = typer.Argument(..., help="git rev-parse --show-toplevel value")):
        """Exit 0 if committing at `toplevel` is allowed under the active task.

        Fail-open on any exception: corrupt state, missing config, etc. -> exit 0.
        """
        try:
            container = get_container()
            state = container.state_manager().load()
        except Exception:
            raise typer.Exit(code=0)

        if state.current_task is None:
            raise typer.Exit(code=0)

        task = state.tasks.get(state.current_task)
        if task is None or not task.worktrees:
            raise typer.Exit(code=0)

        try:
            tl = Path(toplevel).resolve()
            allowed = {Path(p).resolve() for p in task.worktrees.values()}
        except (OSError, RuntimeError):
            raise typer.Exit(code=0)

        if tl in allowed:
            raise typer.Exit(code=0)

        import sys
        sys.stderr.write(
            f"\u26d4 mship: refusing commit — this is not a worktree for the active task '{task.slug}'.\n"
            f"   Expected one of:\n"
        )
        for repo_name in sorted(task.worktrees.keys()):
            wt = Path(task.worktrees[repo_name]).resolve()
            sys.stderr.write(f"     {wt} ({repo_name})\n")
        sys.stderr.write(
            f"   Current: {tl}\n"
            f"   cd into the correct worktree, or use `git commit --no-verify` to override.\n"
        )
        raise typer.Exit(code=1)
