"""Shared audit-gate logic used by mship spawn and mship finish."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from mship.core.repo_state import AuditReport


class AuditGateBlocked(RuntimeError):
    """Raised when the audit gate blocks the command."""


def run_audit_gate(
    report: AuditReport,
    *,
    block: bool,
    force: bool,
    command_name: str,
    on_bypass: Callable[[list[str]], None],
    scope_repos: "frozenset[str] | None" = None,
) -> None:
    """Apply the audit gate.

    - No errors: return silently.
    - Errors + force=True: call on_bypass(codes) and return.
    - Errors + block=True: raise AuditGateBlocked with the issue summary.
    - Errors + block=False: print nothing here; caller is expected to warn.

    `scope_repos` (optional): if provided, only errors in repos that are in
    this set count toward blocking. Errors in out-of-scope repos are skipped.
    Used by `mship finish` (#112) to scope blocking to repos with pending work
    plus their transitive deps. None = no filter (current behavior).
    """
    error_codes: list[str] = []
    for repo in report.repos:
        if scope_repos is not None and repo.name not in scope_repos:
            continue
        for issue in repo.issues:
            if issue.severity == "error":
                error_codes.append(f"{repo.name}:{issue.code}")

    if not error_codes:
        return

    if force:
        on_bypass(error_codes)
        return

    if block:
        raise AuditGateBlocked(
            f"{command_name} blocked by audit — "
            + ", ".join(error_codes)
        )
    # block=False, not forced: caller handles warning


def compute_finish_audit_scope(task, config, graph, pr_mgr) -> "frozenset[str]":
    """Repos whose drift should block `mship finish`. See #112.

    A repo is in scope when either:
    - It is in `task.affected_repos` AND has commits past its effective base
      (i.e. the finish will push it as a PR), OR
    - It is a transitive dependency (via `mothership.yaml#repos.<r>.depends_on`)
      of any in-scope repo (drift in deps can break the task's build/test).

    Repos in `affected_repos` without commits are excluded — they won't
    produce a PR, so workspace-wide drift in them is informational only.
    """
    repos_with_work: set[str] = set()
    for repo_name in task.affected_repos:
        wt = task.worktrees.get(repo_name)
        if wt is None:
            continue
        wt_path = Path(wt)
        if not wt_path.exists():
            continue
        repo_cfg = config.repos.get(repo_name)
        if repo_cfg is None:
            continue
        base = repo_cfg.base_branch or "main"
        if pr_mgr.count_commits_ahead(wt_path, base, task.branch) > 0:
            repos_with_work.add(repo_name)

    in_scope: set[str] = set(repos_with_work)
    for repo_name in repos_with_work:
        in_scope.update(graph.dependencies(repo_name))
    return frozenset(in_scope)


def collect_known_worktree_paths(state_manager) -> "frozenset[Path]":
    """Return a resolved, absolute set of every worktree path in every task."""
    state = state_manager.load()
    paths: set[Path] = set()
    for task in state.tasks.values():
        for raw in task.worktrees.values():
            paths.add(Path(raw).resolve())
    return frozenset(paths)
