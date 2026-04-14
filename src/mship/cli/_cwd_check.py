"""Shared cwd-vs-active-worktree check for log/test/etc."""
from pathlib import Path


def format_cwd_warning(cwd: Path, worktree: Path) -> str | None:
    """Return a warning string if cwd is not inside worktree, else None."""
    try:
        cwd_r = cwd.resolve()
        wt_r = worktree.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        cwd_r.relative_to(wt_r)
        return None  # cwd IS inside the worktree
    except ValueError:
        return (
            f"⚠ running from {cwd_r}, not the active repo's worktree at {wt_r}\n"
            f"  (commands still run in the correct path, but edits in your shell won't affect the worktree)"
        )
