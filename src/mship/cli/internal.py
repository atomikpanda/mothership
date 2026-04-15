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

    @app.command(name="_post-checkout", hidden=True)
    def post_checkout(
        prev_head: str = typer.Argument(..., help="git $1 — previous HEAD"),
        new_head: str = typer.Argument(..., help="git $2 — new HEAD"),
    ):
        """Warn loudly when the agent checks out a branch outside mship's expected flow."""
        import subprocess
        from pathlib import Path

        try:
            container = get_container()
            state = container.state_manager().load()
        except Exception:
            raise typer.Exit(code=0)

        # Current branch (after checkout)
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, cwd=Path.cwd(),
            )
        except Exception:
            raise typer.Exit(code=0)
        if result.returncode != 0:
            raise typer.Exit(code=0)
        current_branch = result.stdout.strip()

        if current_branch in {"main", "master", "develop"}:
            raise typer.Exit(code=0)

        import sys
        if state.current_task is None:
            sys.stderr.write(
                f"\u26a0 mship: checked out '{current_branch}' but no active mship task.\n"
                f"  If you're starting feature work, run `mship spawn \"<description>\"` — "
                f"it'll give you a proper worktree and state.\n"
            )
            raise typer.Exit(code=0)

        task = state.tasks.get(state.current_task)
        if task is None:
            raise typer.Exit(code=0)

        cwd = Path.cwd().resolve()
        in_worktree = any(
            cwd == Path(p).resolve() or cwd.is_relative_to(Path(p).resolve())
            for p in task.worktrees.values()
        )

        if current_branch == task.branch and in_worktree:
            raise typer.Exit(code=0)

        if current_branch != task.branch:
            sys.stderr.write(
                f"\u26a0 mship: checked out '{current_branch}' but active task "
                f"'{task.slug}' is on '{task.branch}'.\n"
                f"  If this was a mistake, `git checkout {task.branch}` in the worktree.\n"
                f"  If you're switching tasks, run `mship close --abandon` first.\n"
            )
            raise typer.Exit(code=0)

        # current_branch == task.branch but cwd isn't in a worktree
        worktree_paths = [str(Path(p).resolve()) for p in task.worktrees.values()]
        primary = worktree_paths[0] if worktree_paths else ""
        sys.stderr.write(
            f"\u26a0 mship: you checked out '{current_branch}' here, but the task's worktree is\n"
            f"  {primary}\n"
            f"  cd there — don't edit in main.\n"
        )
        raise typer.Exit(code=0)
