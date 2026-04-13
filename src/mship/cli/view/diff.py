import shutil
import subprocess
from pathlib import Path
from typing import Iterable

import typer

from mship.cli.view._base import ViewApp
from mship.core.view.diff_sources import collect_worktree_diff


class DiffView(ViewApp):
    def __init__(self, worktree_paths: Iterable[Path], use_delta: bool | None = None, **kw):
        super().__init__(**kw)
        self._paths = list(worktree_paths)
        if use_delta is None:
            use_delta = shutil.which("delta") is not None
        self._use_delta = use_delta

    def _render_body(self, body: str) -> str:
        if not self._use_delta or not body:
            return body
        try:
            result = subprocess.run(
                ["delta", "--color-only"],
                input=body,
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return result.stdout
        except (subprocess.SubprocessError, FileNotFoundError):
            return body

    def gather(self) -> str:
        if not self._paths:
            return "No worktrees configured"
        sections: list[str] = []
        for p in self._paths:
            try:
                wd = collect_worktree_diff(p)
            except subprocess.CalledProcessError as e:
                sections.append(f"▶ {p}  (error: {e})")
                continue
            if wd.files_changed == 0:
                sections.append(f"▶ {p}  (clean)")
                continue
            header = f"▶ {p}  ·  {wd.files_changed} files"
            body = self._render_body(wd.combined)
            sections.append(f"{header}\n{body}")
        return "\n\n".join(sections)


def _collect_workspace_worktrees(container) -> list[Path]:
    """All worktree paths for the current task, plus repo roots if no task."""
    state = container.state_manager().load()
    if state.current_task and state.current_task in state.tasks:
        task = state.tasks[state.current_task]
        paths = [Path(p) for p in task.worktrees.values()]
        if paths:
            return paths
    return [Path(repo.path) for repo in container.config().repos.values()]


def register(app: typer.Typer, get_container):
    @app.command()
    def diff(
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
    ):
        """Live per-worktree git diff, untracked files inline."""
        container = get_container()
        view = DiffView(
            worktree_paths=_collect_workspace_worktrees(container),
            watch=watch,
            interval=interval,
        )
        view.run()
