"""Workspace-discovery marker used by subrepo worktrees.

Each `mship spawn` drops a one-line `.mship-workspace` file in every
worktree it creates. The file's content is the absolute path of the dir
containing `mothership.yaml`. When a user (or a git hook) runs `mship`
from inside a subrepo worktree — a path that isn't an ancestor of the
workspace root — `ConfigLoader.discover` consults this marker to resolve
the workspace. See #84.

The marker is excluded from git via the worktree's per-worktree exclude
file (`<parent-repo>/.git/worktrees/<slug>/info/exclude`) so it doesn't
pollute tracked `.gitignore`.

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


def append_to_worktree_exclude(
    worktree_path: Path, parent_git_dir: Path, slug: str,
) -> bool:
    """Append `MARKER_NAME` to `<parent_git_dir>/worktrees/<slug>/info/exclude`.

    Idempotent — does not duplicate an existing entry. Returns True on
    success; False on any OS error (missing dir, permission denied, etc.)
    so the caller can degrade gracefully.
    """
    try:
        info_dir = Path(parent_git_dir) / "worktrees" / slug / "info"
        # git does not create info/ by default; create it if the parent
        # worktree state dir exists (i.e. <git_dir>/worktrees/<slug>/ is present).
        if not info_dir.is_dir():
            parent = info_dir.parent
            if not parent.is_dir():
                return False
            info_dir.mkdir()
        exclude = info_dir / "exclude"
        existing = exclude.read_text() if exclude.is_file() else ""
        if MARKER_NAME in existing.splitlines():
            return True
        suffix = "" if existing.endswith("\n") or not existing else "\n"
        exclude.write_text(existing + suffix + MARKER_NAME + "\n")
        return True
    except OSError:
        return False
