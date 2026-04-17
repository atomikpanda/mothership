"""Build the workspace-context payload exposed by `mship context`.

A pure function `build_context()` aggregates state, log, and git probes into one
JSON-shaped dict so an agent can recover its full top-of-turn picture in a
single tool call. See GitHub issue #50.

Tier-1 fields only (no network, no `gh` calls). Drift / main-checkout-clean /
last-workspace-fetch are deferred to follow-ups.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from mship.core.config import WorkspaceConfig
from mship.core.log import LogManager
from mship.core.state import Task, WorkspaceState


SCHEMA_VERSION = "1"


GitCounter = Callable[[Path, str], Optional[int]]


def _git_count_default(wt_path: Path, ref_spec: str) -> Optional[int]:
    """`git rev-list --count <ref_spec>` in wt_path, or None on any error."""
    try:
        r = subprocess.run(
            ["git", "rev-list", "--count", ref_spec],
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _last_test_summary(task: Task) -> tuple[Optional[str], int]:
    """Return (status_of_most_recent_repo_test, task.test_iteration).

    Picks the single most-recent TestResult across all repos by `at` timestamp.
    Returns (None, 0) when no tests have run.
    """
    if not task.test_results:
        return (None, task.test_iteration)
    most_recent = max(task.test_results.values(), key=lambda r: r.at)
    return (most_recent.status, task.test_iteration)


def _last_log_at(log_manager: LogManager, slug: str) -> Optional[str]:
    entries = log_manager.read(slug)
    if not entries:
        return None
    return entries[-1].timestamp.isoformat()


def _cwd_match(state: WorkspaceState, cwd: Path) -> tuple[Optional[str], Optional[str]]:
    """Return (task_slug, repo_name) if cwd is inside a known worktree.

    Walks every active task's worktrees; returns the first match. Distinct from
    task_resolver.resolve_task() — we don't want flag/env fallback here, only
    a literal cwd match.
    """
    cwd_resolved = cwd.resolve()
    for task in state.tasks.values():
        if task.finished_at is not None:
            continue
        for repo, wt_path in task.worktrees.items():
            try:
                wt_resolved = Path(wt_path).resolve()
            except (OSError, RuntimeError):
                continue
            try:
                cwd_resolved.relative_to(wt_resolved)
                return (task.slug, repo)
            except ValueError:
                continue
    return (None, None)


def _binary_matches_editable_install() -> Optional[bool]:
    """True iff `mship` on PATH lives in the venv the current process is using.

    Catches the foot-gun where the user edits source under `uv run mship` but
    a separately-installed `mship` (e.g. `uv tool install mothership`) is what
    sits on PATH — same name, different code. Returns None when there's no
    `mship` on PATH at all (so the field can stay informative without lying).
    """
    on_path = shutil.which("mship")
    if on_path is None:
        return None
    expected = Path(sys.prefix) / "bin" / "mship"
    on_path_p = Path(on_path)
    if not expected.exists():
        return False
    try:
        return on_path_p.resolve().samefile(expected.resolve())
    except OSError:
        return False


def _task_payload(
    task: Task,
    log_manager: LogManager,
    git_count: GitCounter,
) -> dict[str, Any]:
    last_test_state, last_test_iteration = _last_test_summary(task)

    ahead_of_origin: dict[str, Optional[int]] = {}
    ahead_of_base: dict[str, Optional[int]] = {}
    for repo, wt_path in task.worktrees.items():
        wt = Path(wt_path)
        ahead_of_origin[repo] = git_count(wt, "@{u}..HEAD")
        if task.base_branch:
            ahead_of_base[repo] = git_count(wt, f"{task.base_branch}..HEAD")
        else:
            ahead_of_base[repo] = None

    return {
        "slug": task.slug,
        "branch": task.branch,
        "base_branch": task.base_branch,
        "phase": task.phase,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "worktrees": {repo: str(p) for repo, p in task.worktrees.items()},
        "active_repo": task.active_repo,
        "ahead_of_origin": ahead_of_origin,
        "ahead_of_base": ahead_of_base,
        "pr_urls": dict(task.pr_urls),
        "last_test_state": last_test_state,
        "last_test_iteration": last_test_iteration,
        "last_log_entry_at": _last_log_at(log_manager, task.slug),
    }


def build_context(
    state: WorkspaceState,
    config: WorkspaceConfig,
    log_manager: LogManager,
    cwd: Path,
    *,
    git_count: GitCounter = _git_count_default,
    binary_check: Callable[[], Optional[bool]] = _binary_matches_editable_install,
) -> dict[str, Any]:
    """Assemble the agent-readable workspace context payload.

    `git_count` and `binary_check` are injection points for tests; production
    callers leave them at the defaults.
    """
    del config  # reserved for Tier-2 fields (main_checkout_clean, etc.)

    active_tasks = [
        _task_payload(task, log_manager, git_count)
        for task in state.tasks.values()
        if task.finished_at is None
    ]

    cwd_task, cwd_repo = _cwd_match(state, cwd)

    return {
        "schema_version": SCHEMA_VERSION,
        "active_tasks": active_tasks,
        "cwd_matches_task": cwd_task,
        "cwd_matches_repo": cwd_repo,
        "mship_binary_matches_editable_install": binary_check(),
    }
