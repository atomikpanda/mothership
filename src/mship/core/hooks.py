"""Install / uninstall / detect the mship pre-commit hook.

The hook is a small POSIX shell block wrapped in MSHIP-BEGIN/END markers so it
coexists with user hooks. We never overwrite foreign content; we append or
strip our block as needed.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path


HOOK_MARKER_BEGIN = "# MSHIP-BEGIN"
HOOK_MARKER_END = "# MSHIP-END"

HOOK_BLOCK = f"""{HOOK_MARKER_BEGIN} — managed by mship; edit outside this block is fine
if command -v mship >/dev/null 2>&1; then
    toplevel="$(git rev-parse --show-toplevel)"
    mship _check-commit "$toplevel" || exit 1
fi
{HOOK_MARKER_END}
"""

_NEW_FILE_TEMPLATE = f"""#!/bin/sh
# git pre-commit hook
{HOOK_BLOCK}"""


def _hook_path(git_root: Path) -> Path:
    return git_root / ".git" / "hooks" / "pre-commit"


def is_installed(git_root: Path) -> bool:
    """True if the hook file contains our marker block."""
    path = _hook_path(git_root)
    if not path.exists():
        return False
    return HOOK_MARKER_BEGIN in path.read_text()


def install_hook(git_root: Path) -> None:
    """Install the hook at `<git_root>/.git/hooks/pre-commit`.

    Idempotent when our marker is already present. If a user hook exists
    without our marker, append the MSHIP block after the existing content.
    """
    hooks_dir = git_root / ".git" / "hooks"
    if not hooks_dir.exists():
        # .git/hooks is created by `git init`; if it's missing, this isn't
        # a git repo (or an unusual setup). Don't fabricate it.
        raise FileNotFoundError(f"git hooks dir not found: {hooks_dir}")

    path = _hook_path(git_root)

    if path.exists():
        content = path.read_text()
        if HOOK_MARKER_BEGIN in content:
            _chmod_executable(path)
            return
        # Append MSHIP block after existing content. Guarantee a blank line first.
        if not content.endswith("\n"):
            content += "\n"
        if not content.endswith("\n\n"):
            content += "\n"
        content += HOOK_BLOCK
        path.write_text(content)
    else:
        path.write_text(_NEW_FILE_TEMPLATE)

    _chmod_executable(path)


def uninstall_hook(git_root: Path) -> None:
    """Remove our MSHIP block from the hook file, preserving any user content.

    No-op if the file is missing or doesn't contain our marker.
    """
    path = _hook_path(git_root)
    if not path.exists():
        return
    content = path.read_text()
    if HOOK_MARKER_BEGIN not in content:
        return

    begin_idx = content.index(HOOK_MARKER_BEGIN)
    # Find the END marker and the newline after it
    end_search = content.find(HOOK_MARKER_END, begin_idx)
    if end_search == -1:
        # Marker opened but not closed — bail conservatively, don't mutate
        return
    after_end = content.find("\n", end_search)
    after_end = len(content) if after_end == -1 else after_end + 1

    # Also swallow a blank separator line before the block, if present
    cut_start = begin_idx
    if cut_start >= 2 and content[cut_start - 2:cut_start] == "\n\n":
        cut_start -= 1  # keep one newline, drop the extra

    new_content = content[:cut_start] + content[after_end:]
    # Avoid a dangling extra trailing newline
    while new_content.endswith("\n\n"):
        new_content = new_content[:-1]
    path.write_text(new_content)


def _chmod_executable(path: Path) -> None:
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
