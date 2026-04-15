"""Install / uninstall / detect mship git hooks.

Each hook is a small POSIX shell block wrapped in MSHIP-BEGIN/END markers so it
coexists with user hooks. We never overwrite foreign content; we append or
strip our block as needed.
"""
from __future__ import annotations

import stat
from pathlib import Path


HOOK_MARKER_BEGIN = "# MSHIP-BEGIN"
HOOK_MARKER_END = "# MSHIP-END"


def _block(body_sh: str) -> str:
    return (
        f"{HOOK_MARKER_BEGIN} — managed by mship; edit outside this block is fine\n"
        f"{body_sh}"
        f"{HOOK_MARKER_END}\n"
    )


_PRE_COMMIT_BODY = """if command -v mship >/dev/null 2>&1; then
    toplevel="$(git rev-parse --show-toplevel)"
    mship _check-commit "$toplevel" || exit 1
fi
"""

_POST_CHECKOUT_BODY = """if command -v mship >/dev/null 2>&1; then
    prev_head="$1"
    new_head="$2"
    is_branch_checkout="$3"
    if [ "$is_branch_checkout" = "1" ]; then
        mship _post-checkout "$prev_head" "$new_head" || true
    fi
fi
"""

_POST_COMMIT_BODY = """if command -v mship >/dev/null 2>&1; then
    mship _log-commit || true
fi
"""


# Public hook inventory — name → (file header comment, body)
_HOOKS: dict[str, tuple[str, str]] = {
    "pre-commit": ("# git pre-commit hook", _PRE_COMMIT_BODY),
    "post-checkout": ("# git post-checkout hook", _POST_CHECKOUT_BODY),
    "post-commit": ("# git post-commit hook", _POST_COMMIT_BODY),
}


def _hook_path(git_root: Path, name: str) -> Path:
    return git_root / ".git" / "hooks" / name


def _install_one(git_root: Path, name: str, header: str, body_sh: str) -> None:
    hooks_dir = git_root / ".git" / "hooks"
    if not hooks_dir.exists():
        raise FileNotFoundError(f"git hooks dir not found: {hooks_dir}")

    path = _hook_path(git_root, name)
    block = _block(body_sh)

    if path.exists():
        content = path.read_text()
        if HOOK_MARKER_BEGIN in content:
            _chmod_executable(path)
            return
        if not content.endswith("\n"):
            content += "\n"
        if not content.endswith("\n\n"):
            content += "\n"
        content += block
        path.write_text(content)
    else:
        path.write_text(f"#!/bin/sh\n{header}\n{block}")

    _chmod_executable(path)


def _uninstall_one(git_root: Path, name: str) -> None:
    path = _hook_path(git_root, name)
    if not path.exists():
        return
    content = path.read_text()
    if HOOK_MARKER_BEGIN not in content:
        return

    begin_idx = content.index(HOOK_MARKER_BEGIN)
    end_search = content.find(HOOK_MARKER_END, begin_idx)
    if end_search == -1:
        return
    after_end = content.find("\n", end_search)
    after_end = len(content) if after_end == -1 else after_end + 1

    cut_start = begin_idx
    if cut_start >= 2 and content[cut_start - 2:cut_start] == "\n\n":
        cut_start -= 1

    new_content = content[:cut_start] + content[after_end:]
    while new_content.endswith("\n\n"):
        new_content = new_content[:-1]
    path.write_text(new_content)


def _one_is_installed(git_root: Path, name: str) -> bool:
    path = _hook_path(git_root, name)
    if not path.exists():
        return False
    return HOOK_MARKER_BEGIN in path.read_text()


# --- Public API ---

def is_installed(git_root: Path) -> bool:
    """True if ALL three hooks contain our marker block."""
    return all(_one_is_installed(git_root, name) for name in _HOOKS)


def install_hook(git_root: Path) -> None:
    """Install pre-commit, post-checkout, and post-commit hooks at `<git_root>/.git/hooks/`.

    Idempotent. Appends to existing user content; never overwrites.
    """
    for name, (header, body) in _HOOKS.items():
        _install_one(git_root, name, header, body)


def uninstall_hook(git_root: Path) -> None:
    """Remove our MSHIP block from all three hook files, preserving user content."""
    for name in _HOOKS:
        _uninstall_one(git_root, name)


def _chmod_executable(path: Path) -> None:
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# Preserved for backward compat with any external caller of the old name.
HOOK_BLOCK = _block(_PRE_COMMIT_BODY)
