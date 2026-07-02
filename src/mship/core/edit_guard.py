"""Decide whether an edit may land at a given path while tasks are active.

No env, no git — the CLI adapter in cli/internal.py handles stdin/JSON/env/
exit-code; this module only answers allow-or-block. Prevents the failure mode
where an agent edits a repo's MAIN checkout (reachable via a symlink or
absolute path) instead of the task worktree, silently landing work on the base
branch. See spec guard-against-editing-a-repos-main.

Also enforces the WorkItem gate (core/workitem_gate.py::check_task_gate) for
edits that DO land in the right worktree: every task must carry a WorkItem,
and a feature-kind WorkItem needs an approved spec. This is opt-in via the
`workspace_root` parameter (it does do filesystem I/O to load the WorkItem/spec
stores) — omit it to keep the old, location-only behavior. See spec
workitem-mandatory-kind-gated-approval.
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


def _gate_worktree_edit(task, workspace_root, bypass_workitem_gate: bool) -> GuardDecision:
    """An edit landing inside the task's OWN worktree is the legitimate
    location, but the task must still carry a WorkItem (feature ⇒ approved
    spec) — core/workitem_gate.py::check_task_gate. Skipped (allowed) when
    `workspace_root` is unknown (callers that don't pass it opt out of this
    check entirely) or when the caller has resolved a MSHIP_BYPASS_GATE
    hotfix escape. Fails OPEN on any unexpected error — never block an edit
    over a gate bug."""
    if workspace_root is None or bypass_workitem_gate:
        return GuardDecision(allowed=True)
    try:
        from mship.core.workitem_gate import check_task_gate
        result = check_task_gate(task, workspace_root)
    except Exception:
        return GuardDecision(allowed=True)  # fail open
    if result.ok:
        return GuardDecision(allowed=True)
    return GuardDecision(
        allowed=False,
        reason=(
            f"Task '{task.slug}' is missing its WorkItem gate clearance: "
            f"{result.reason}\n(set MSHIP_BYPASS_GATE=1 to override.)"
        ),
    )


def evaluate_edit(
    target, state, config,
    workspace_root=None,
    bypass_workitem_gate: bool = False,
) -> GuardDecision:
    """Block an edit whose realpath is inside a repo's main checkout while that
    repo has an active task and the path is not inside that task's worktree.
    Allows everything else (caller fails open on errors).

    `workspace_root`: when given, an edit that lands inside the owning task's
    worktree is additionally passed through the WorkItem gate (see module
    docstring); omit to keep the old, location-only behavior.
    `bypass_workitem_gate`: the MSHIP_BYPASS_GATE hotfix escape for that gate
    (resolved by the caller via core/gate.py::resolve_bypass) — does not affect
    the main-checkout block above, which still requires MSHIP_ALLOW_MAIN_EDIT.
    """
    rp = _real(Path(target))

    # An edit that lands inside ANY active task's own worktree is the
    # legitimate location (this is independent of — and checked before — the
    # main-checkout matching below, since a task's worktree normally lives as
    # a sibling of the repo's main checkout, not nested inside it, so it would
    # never match the main-checkout loop at all). Still gated on the task
    # carrying a WorkItem. See module docstring.
    for task in state.tasks.values():
        for wt in task.worktrees.values():
            if _within(rp, _real(Path(wt))):
                return _gate_worktree_edit(task, workspace_root, bypass_workitem_gate)

    # Among repos whose main checkout contains the target, the MOST-SPECIFIC one
    # (deepest path) owns the file — so a nested repo (e.g. `web` inside
    # `backend`'s tree) is judged by its OWN active-task state, not a parent
    # repo's. This is independent of config declaration order.
    owner_name = None
    owner_main = None
    for name, repo in config.repos.items():
        main = _real(Path(repo.path))
        if not _within(rp, main):
            continue
        if owner_main is None or len(main.parts) > len(owner_main.parts):
            owner_name, owner_main = name, main
    if owner_name is None:
        return GuardDecision(allowed=True)  # not inside any repo's main checkout

    # Collect every active task that owns a worktree for the owning repo. An edit
    # inside ANY of those worktrees is legitimate — but the loop above already
    # returned for that case, so anything reaching here is NOT inside any of
    # them; these are candidates for the "edit here instead" suggestion below.
    candidates = []  # list[(slug, worktree_realpath)]
    for slug, task in state.tasks.items():
        if owner_name not in task.affected_repos:
            continue
        wt = task.worktrees.get(owner_name)
        if wt is None:
            continue
        candidates.append((slug, _real(Path(wt))))
    if not candidates:
        return GuardDecision(allowed=True)  # the owning repo has no active task

    try:
        rel = rp.relative_to(owner_main)
    except ValueError:
        rel = None

    def _suggest(wt_real):
        return wt_real / rel if rel is not None else wt_real

    if len(candidates) == 1:
        slug, wt_real = candidates[0]
        reason = (
            f"Editing the MAIN checkout of '{owner_name}' while task "
            f"'{slug}' is active. Edit here instead:\n  {_suggest(wt_real)}\n"
            f"(set MSHIP_ALLOW_MAIN_EDIT=1 to override.)"
        )
    else:
        listing = "\n".join(
            f"  '{slug}': {_suggest(wt_real)}" for slug, wt_real in candidates
        )
        reason = (
            f"Editing the MAIN checkout of '{owner_name}' while multiple tasks are "
            f"active for it. Edit in the appropriate task worktree:\n{listing}\n"
            f"(set MSHIP_ALLOW_MAIN_EDIT=1 to override.)"
        )
    return GuardDecision(allowed=False, reason=reason)
