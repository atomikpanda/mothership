"""Lifecycle hooks: react to task/WorkItem/PR state transitions by running a
configured go-task target or shell command.

NOT git hooks — see mship.core.hooks for the unrelated pre-commit/pre-push/
post-checkout/post-commit installer. This module is the runtime dispatcher for
the declarative `hooks:` list in mothership.yaml (mship.core.config.HookConfig)
— see spec mship-lifecycle-hooks (MOS-220) for the full event catalog and
design rationale.

Execution model (v1, deliberately simple — see spec open question q3):
synchronous, one hook at a time, each bounded by its own timeout. Async /
fire-and-forget execution is an explicit v2 follow-up, not this slice.

Failure handling is fail-open by default: a hook that raises, exits nonzero,
or times out is caught, logged as a warning, and reported back via its
HookResult — but does NOT raise, so a bad hook can never wedge a task/WorkItem
transition. `required: true` (mship.core.config.HookConfig.required) opts a
specific hook into blocking behavior: its failure raises HookRequiredError
instead, which callers at blocking-capable wire points (phase.entered.*,
workitem.phase.*) should let propagate to abort the transition *before* it
commits — those are the only two event families that fire BEFORE their state
mutation commits, so a required failure there can genuinely block it.

`required: true` is rejected at config-load time (see config.py) for every
other v1 event — task.finished, task.closed, pr.merged, pr.closed — because
each of those fires only AFTER its own irreversible side effects already
landed (PRs/branches pushed, spec state advanced, worktree torn down, or
simply observed well after the fact by PrWatcher's poll). A required failure
there can't cleanly abort/roll back anything; it would just leave partial
state (e.g. a spec already advanced while the task stays open). So those four
events are always fail-open, regardless of `required:`.

A hook's `run` executes through the exact same env_runner-wrapped shell path
executor.py uses for go-task targets (ShellRunner.build_command +
ShellRunner.run) — no new sandbox surface. cwd/env_runner resolution mirrors
RepoExecutor's: a hook's own `repo:` (or, if omitted, the firing event's
context repo) picks the repo config; a task_slug's active worktree for that
repo wins over the repo's plain path; no repo at all falls back to the
workspace root + the workspace-level env_runner.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mship.util.shell import ShellRunner

if TYPE_CHECKING:
    from mship.core.config import HookConfig, WorkspaceConfig

log = logging.getLogger(__name__)


class HookRequiredError(RuntimeError):
    """Raised when a `required: true` hook fails (nonzero exit or timeout).

    Callers at blocking-capable wire points (phase.entered.*,
    workitem.phase.*) should let this propagate to abort the transition
    before it commits. Never raised for task.finished, task.closed,
    pr.merged, or pr.closed — `required: true` is rejected for those
    post-hoc events at config-load time (see config.py's `_POST_HOC_EVENTS`),
    since each fires only after its own irreversible side effects already
    landed, so there is nothing left to block."""


@dataclass
class HookResult:
    hook_name: str
    event: str
    ok: bool
    timed_out: bool = False
    error: str | None = None
    returncode: int | None = None


@dataclass
class HookContext:
    """Event-specific defaults a hook inherits when its config entry omits
    `repo:`. Populated fields depend on the firing event:
    - task.finished / task.closed / phase.entered.*: `task_slug` (a task can
      span multiple repos, so no default `repo` is inferred — a hook that
      needs one repo-scoped cwd/env_runner must set `repo:` explicitly).
    - workitem.phase.*: `workitem_id`.
    - pr.merged / pr.closed: `repo` (the PR's own repo) and `task_slug`.
    """
    task_slug: str | None = None
    repo: str | None = None
    workitem_id: str | None = None


def _matching_hooks(event: str, config: "WorkspaceConfig") -> list["HookConfig"]:
    return [h for h in config.hooks if h.on == event]


def _resolve_repo_cwd(
    repo_name: str, config: "WorkspaceConfig", state_manager: Any, task_slug: str | None,
) -> Path:
    """Mirror RepoExecutor._resolve_cwd: prefer the task's worktree for this
    repo (if it exists on disk), else the repo's resolved path (following
    git_root for subdirectory services)."""
    repo_config = config.repos[repo_name]
    if task_slug and state_manager is not None:
        state = state_manager.load()
        task = state.tasks.get(task_slug)
        if task is not None:
            wt = task.worktrees.get(repo_name)
            if wt is not None:
                wt_path = Path(wt)
                if wt_path.exists():
                    return wt_path
    if repo_config.git_root is not None:
        parent = config.repos[repo_config.git_root]
        return parent.path / repo_config.path
    return repo_config.path


def _resolve_target(
    repo_name: str | None,
    config: "WorkspaceConfig",
    workspace_root: Path,
    state_manager: Any,
    task_slug: str | None,
) -> tuple[Path, str | None]:
    if repo_name is not None and repo_name in config.repos:
        repo_config = config.repos[repo_name]
        env_runner = (
            repo_config.env_runner if repo_config.env_runner is not None
            else config.env_runner
        )
        cwd = _resolve_repo_cwd(repo_name, config, state_manager, task_slug)
        return cwd, env_runner
    return workspace_root, config.env_runner


def _run_one(
    hook: "HookConfig",
    event: str,
    *,
    config: "WorkspaceConfig",
    workspace_root: Path,
    shell: ShellRunner,
    state_manager: Any,
    context: HookContext,
) -> HookResult:
    name = hook.name or hook.run
    repo_name = hook.repo or context.repo
    timeout = hook.timeout if hook.timeout is not None else config.hooks_default_timeout

    cwd, env_runner = _resolve_target(
        repo_name, config, workspace_root, state_manager, context.task_slug,
    )
    command = shell.build_command(hook.run, env_runner)

    try:
        shell_result = shell.run(command, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return HookResult(
            hook_name=name, event=event, ok=False, timed_out=True,
            error=f"timed out after {timeout}s",
        )
    except Exception as e:  # a hook target that can't even launch (bad cwd, etc)
        return HookResult(hook_name=name, event=event, ok=False, error=str(e))

    if shell_result.returncode != 0:
        stderr = (shell_result.stderr or "").strip()
        detail = f": {stderr[:500]}" if stderr else ""
        return HookResult(
            hook_name=name, event=event, ok=False,
            returncode=shell_result.returncode,
            error=f"exited {shell_result.returncode}{detail}",
        )

    return HookResult(hook_name=name, event=event, ok=True, returncode=0)


def run_hooks(
    event: str,
    context: HookContext | None = None,
    *,
    config: "WorkspaceConfig",
    workspace_root: Path,
    shell: ShellRunner | None = None,
    state_manager: Any | None = None,
) -> list[HookResult]:
    """Run every `hooks:` entry configured `on: <event>`, synchronously, each
    bounded by its (or the workspace default) timeout.

    Fail-open by default: a failing/timing-out non-required hook is logged as
    a warning and reported in the returned list with `ok=False`; run_hooks
    itself does not raise for it. A `required: true` hook's failure raises
    HookRequiredError immediately (subsequent hooks for this event do not
    run) — callers at blocking-capable wire points should let it propagate.

    Returns an empty list (no shell calls at all) when no hook matches
    `event` — the common case for most transitions in a workspace that
    hasn't configured any hooks for them.
    """
    context = context or HookContext()
    shell = shell or ShellRunner()
    results: list[HookResult] = []

    for hook in _matching_hooks(event, config):
        result = _run_one(
            hook, event,
            config=config, workspace_root=workspace_root,
            shell=shell, state_manager=state_manager, context=context,
        )
        results.append(result)
        if not result.ok:
            if hook.required:
                raise HookRequiredError(
                    f"required hook {result.hook_name!r} for event {event!r} "
                    f"failed: {result.error}"
                )
            log.warning(
                "lifecycle hook %r for event %r failed (non-blocking): %s",
                result.hook_name, event, result.error,
            )

    return results
