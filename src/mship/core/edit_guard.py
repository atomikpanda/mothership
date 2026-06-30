"""Decide whether an edit may land at a given path while tasks are active.

Pure — no I/O, no env, no git. The CLI adapter in cli/internal.py handles
stdin/JSON/env/exit-code; this module only answers allow-or-block. Prevents the
failure mode where an agent edits a repo's MAIN checkout (reachable via a
symlink or absolute path) instead of the task worktree, silently landing work on
the base branch. See spec guard-against-editing-a-repos-main.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str = ""


def _real(p: Path) -> Path:
    # realpath resolves symlinks AND a not-yet-existing leaf (Write of a new
    # file): the existing prefix is canonicalized, the rest appended verbatim.
    return Path(os.path.realpath(str(p)))


def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def evaluate_edit(target, state, config) -> GuardDecision:
    """Block an edit whose realpath is inside a repo's main checkout while that
    repo has an active task and the path is not inside that task's worktree.
    Allows everything else (caller fails open on errors)."""
    rp = _real(Path(target))
    for name, repo in config.repos.items():
        main = _real(Path(repo.path))
        if not _within(rp, main):
            continue
        # Collect every active task that owns a worktree for this repo. An edit
        # inside ANY of those worktrees is legitimate, so allow it outright.
        candidates = []  # list[(slug, worktree_realpath)]
        for slug, task in state.tasks.items():
            if name not in task.affected_repos:
                continue
            wt = task.worktrees.get(name)
            if wt is None:
                continue
            wt_real = _real(Path(wt))
            if _within(rp, wt_real):
                return GuardDecision(allowed=True)
            candidates.append((slug, wt_real))
        if not candidates:
            continue  # repo has no active task — not our concern
        try:
            rel = rp.relative_to(main)
        except ValueError:
            rel = None

        def _suggest(wt_real):
            return wt_real / rel if rel is not None else wt_real

        if len(candidates) == 1:
            slug, wt_real = candidates[0]
            reason = (
                f"Editing the MAIN checkout of '{name}' while task "
                f"'{slug}' is active. Edit here instead:\n  {_suggest(wt_real)}\n"
                f"(set MSHIP_ALLOW_MAIN_EDIT=1 to override.)"
            )
        else:
            listing = "\n".join(
                f"  '{slug}': {_suggest(wt_real)}" for slug, wt_real in candidates
            )
            reason = (
                f"Editing the MAIN checkout of '{name}' while multiple tasks are "
                f"active for it. Edit in the appropriate task worktree:\n{listing}\n"
                f"(set MSHIP_ALLOW_MAIN_EDIT=1 to override.)"
            )
        return GuardDecision(allowed=False, reason=reason)
    return GuardDecision(allowed=True)
