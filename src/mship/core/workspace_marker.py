"""Workspace-discovery marker used by subrepo worktrees.

Each `mship spawn` drops a one-line `.mship-workspace` file in every
worktree it creates. The file's content is the absolute path of the dir
containing `mothership.yaml`. When a user (or a git hook) runs `mship`
from inside a subrepo worktree — a path that isn't an ancestor of the
workspace root — `ConfigLoader.discover` consults this marker to resolve
the workspace. See #84.

The marker is added to the repo's tracked `.gitignore` at spawn time so
it doesn't pollute `git status`. Per-worktree `info/exclude` was tried
first but git resolves `info/exclude` to the shared main-repo path, so
per-worktree excludes don't take effect for worktree-only files. See
#107.

Stale markers (pointing to a missing path or a path without
`mothership.yaml`) return None from `read_marker_from_ancestor`, which
lets `discover` fall through to the usual walk-up — no error, no warning.
"""
from __future__ import annotations

from pathlib import Path


MARKER_NAME = ".mship-workspace"


def write_marker(worktree_path: Path, workspace_root: Path) -> None:
    """Write `<worktree_path>/.mship-workspace` containing the workspace root.

    Always overwrites; one-line path, no trailing metadata.
    """
    worktree_path = Path(worktree_path)
    (worktree_path / MARKER_NAME).write_text(str(Path(workspace_root).resolve()) + "\n")


def read_marker_from_ancestor(start: Path) -> Path | None:
    """Walk up from `start` looking for `.mship-workspace`.

    When found, read the path it points at. If that path exists AND contains
    `mothership.yaml`, return the resolved directory. Otherwise return None
    (stale marker; caller should fall through to another discovery step).
    """
    try:
        current = Path(start).resolve()
    except OSError:
        return None
    while True:
        marker = current / MARKER_NAME
        if marker.is_file():
            try:
                target = Path(marker.read_text().strip())
            except OSError:
                return None
            if target.is_dir() and (target / "mothership.yaml").is_file():
                return target.resolve()
            return None  # stale
        parent = current.parent
        if parent == current:
            return None
        current = parent


