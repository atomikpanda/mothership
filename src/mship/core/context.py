"""Build the workspace-context payload exposed by `mship context`.

A pure function `build_context()` aggregates state, log, and git probes into one
JSON-shaped dict so an agent can recover its full top-of-turn picture in a
single tool call. See GitHub issue #50.

Drift is read from the existing reconcile cache only — `mship context` never
fetches. Stale or absent → `"unknown"` for that task.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from mship.core.base_resolver import resolve_base
from mship.core.config import WorkspaceConfig
from mship.core.log import LogManager
from mship.core.reconcile.cache import ReconcileCache
from mship.core.state import Task, WorkspaceState
from mship.core.workspace_meta import read_last_sync_at


SCHEMA_VERSION = "1"


GitCounter = Callable[[Path, str], Optional[int]]
DirtyCheck = Callable[[Path], Optional[bool]]


# --- MOS-100: --for/--kind audience-shaped output --------------------------
#
# `instructions` text below is STATIC and hand-written -- no LLM, no
# inference, no synthesized summaries. Each string is a fixed constant keyed
# by (--for, --kind); build_audience_block() only ever selects one verbatim.
# The implementer/spec-reviewer/code-quality-reviewer framings are drawn from
# the language already proven out in
# `src/mship/skills/subagent-driven-development/{implementer,spec-reviewer,
# code-quality-reviewer}-prompt.md` so a later migration of those templates
# to consume this output is a drop-in swap rather than a rewrite.

FOR_VALUES: tuple[str, ...] = ("claude-code", "codex", "human", "reviewer")
KIND_VALUES: tuple[str, ...] = ("spec", "code-quality")


class AudienceError(ValueError):
    """Invalid `--for`/`--kind` combination (MOS-100).

    The single source of truth for this validation: `build_context()` always
    runs it via `build_audience_block()`, so the CLI (or any other caller)
    doesn't need its own duplicate check that could drift out of sync.
    """


_IMPLEMENTER_INSTRUCTIONS = (
    "You are implementing this task. Work from the resolved task's worktree "
    "(see this payload's `active_tasks[].worktrees` / `resolved_task`), never "
    "the main checkout. Never commit to `main` -- the mship pre-commit hook "
    "will refuse it, but don't waste a cycle finding that out. Commit your "
    "changes with `mship commit \"<message>\"`, not raw `git commit`, so the "
    "commit lands on the task's feature branch and gets journaled "
    "automatically. When you're investigating something unexpected (a bug, "
    "a failing test, unclear behavior), log your working hypothesis with "
    "`mship debug hypothesis \"<text>\"` before you start changing code."
)

_HUMAN_INSTRUCTIONS = (
    "This is a factual status summary of the workspace: active tasks, their "
    "branches and worktrees, how far each is ahead of or behind its base and "
    "origin, last test results, and phase. Read it as a status report of "
    "what's true right now, not as directives -- it does not tell you what "
    "to do next."
)

_REVIEWER_SPEC_INSTRUCTIONS = (
    "You are reviewing this task for spec compliance. Do not trust the "
    "implementer's self-report on its own -- verify by reading the actual "
    "diff and comparing it line by line against the task description and "
    "its plan. Flag anything under-built (requirements skipped or only "
    "partially done) and anything over-built (scope that wasn't requested). "
    "Report specific file:line references for any gap you find."
)

_REVIEWER_CODE_QUALITY_INSTRUCTIONS = (
    "You are reviewing this task for code quality (after spec compliance "
    "has already passed -- don't re-litigate that here). Inspect the diff "
    "for maintainability (clear responsibilities, no unnecessary "
    "complexity), naming (clear, accurate names matching what things do), "
    "test quality (tests verify real behavior, not mocks, and cover edge "
    "cases), and regressions (existing behavior this change may have broken)."
)

_INSTRUCTIONS: dict[tuple[str, Optional[str]], str] = {
    ("claude-code", None): _IMPLEMENTER_INSTRUCTIONS,
    ("codex", None): _IMPLEMENTER_INSTRUCTIONS,
    ("human", None): _HUMAN_INSTRUCTIONS,
    ("reviewer", "spec"): _REVIEWER_SPEC_INSTRUCTIONS,
    ("reviewer", "code-quality"): _REVIEWER_CODE_QUALITY_INSTRUCTIONS,
}


def _validate_audience(for_: Optional[str], kind: Optional[str]) -> None:
    """Raise AudienceError for any invalid `--for`/`--kind` combination.

    Valid: `for_` is None (kind must also be None); `for_` is claude-code /
    codex / human (kind must be None); `for_` is reviewer (kind must be spec
    or code-quality).
    """
    if for_ is None:
        if kind is not None:
            raise AudienceError(
                f"--kind requires --for reviewer; got --kind {kind!r} with no --for."
            )
        return
    if for_ not in FOR_VALUES:
        raise AudienceError(
            f"unknown --for {for_!r}; expected one of: {', '.join(FOR_VALUES)}."
        )
    if for_ == "reviewer":
        if kind is None:
            raise AudienceError(
                "--for reviewer requires --kind (spec | code-quality)."
            )
        if kind not in KIND_VALUES:
            raise AudienceError(
                f"unknown --kind {kind!r}; expected one of: {', '.join(KIND_VALUES)}."
            )
    elif kind is not None:
        raise AudienceError(
            f"--kind is only valid with --for reviewer; got --for {for_!r} with --kind {kind!r}."
        )


def build_audience_block(
    for_: Optional[str], kind: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Validate `for_`/`kind` and assemble the static `audience` block.

    Returns None when `for_` is None -- the default, no-audience-requested
    path that keeps `mship context`'s output byte-for-byte identical to
    before MOS-100. Raises `AudienceError` for any invalid combination.
    """
    _validate_audience(for_, kind)
    if for_ is None:
        return None
    return {"for": for_, "kind": kind, "instructions": _INSTRUCTIONS[(for_, kind)]}


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


def _dirty_check_default(repo_path: Path) -> Optional[bool]:
    """True if `git status --porcelain` shows any output, False if clean.

    Returns None when the path isn't a git checkout or git errors out — keeps
    the field informative without lying about state we couldn't observe.
    """
    if not repo_path.is_dir():
        return None
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    return bool(r.stdout.strip())


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


def _drift_for_task(slug: str, cache: Optional[ReconcileCache]) -> str:
    if cache is None:
        return "unknown"
    payload = cache.read()
    if payload is None:
        return "unknown"
    raw = payload.results.get(slug)
    if not isinstance(raw, dict):
        return "unknown"
    state = raw.get("state")
    return state if isinstance(state, str) else "unknown"


def _last_drift_check_at(cache: Optional[ReconcileCache]) -> Optional[str]:
    if cache is None:
        return None
    payload = cache.read()
    if payload is None:
        return None
    return datetime.fromtimestamp(payload.fetched_at, tz=timezone.utc).isoformat()


def _main_checkout_clean(
    config: WorkspaceConfig,
    dirty_check: DirtyCheck,
) -> dict[str, Optional[bool]]:
    """Per-repo cleanliness of the main checkout (the path declared in mothership.yaml).

    Skips repos with `git_root` set — they share a checkout with their parent,
    so checking the parent already covers them and a separate entry would
    misleadingly report the parent's status under each child's key.
    """
    out: dict[str, Optional[bool]] = {}
    for name, repo in config.repos.items():
        if repo.git_root is not None:
            continue
        dirty = dirty_check(repo.path)
        out[name] = (not dirty) if dirty is not None else None
    return out


def _effective_base_for_repo(
    task: Task, repo: str, config: WorkspaceConfig,
) -> Optional[str]:
    """Resolve the effective base branch for `repo` via the canonical resolver.

    Falls back to `task.base_branch` when resolve_base has nothing to go on
    (no repo config, no `--base` override) — this preserves the pre-MOS-229
    behavior for repos/workspaces that don't declare `base_branch:` in
    mothership.yaml, and tolerates a repo missing from config entirely
    (`config.repos.get` returns None; resolve_base treats that as "no config").
    """
    repo_config = config.repos.get(repo)
    resolved = resolve_base(
        repo, repo_config, cli_base=None, base_map={},
        known_repos=config.repos.keys(), task_base=task.base_override,
    )
    return resolved if resolved is not None else task.base_branch


def _task_payload(
    task: Task,
    log_manager: LogManager,
    git_count: GitCounter,
    cache: Optional[ReconcileCache],
    config: WorkspaceConfig,
) -> dict[str, Any]:
    last_test_state, last_test_iteration = _last_test_summary(task)

    ahead_of_origin: dict[str, Optional[int]] = {}
    ahead_of_base: dict[str, Optional[int]] = {}
    base_behind_origin: dict[str, Optional[int]] = {}
    for repo, wt_path in task.worktrees.items():
        wt = Path(wt_path)
        ahead_of_origin[repo] = git_count(wt, "@{u}..HEAD")
        eff_base = _effective_base_for_repo(task, repo, config)
        if eff_base:
            ahead_of_base[repo] = git_count(wt, f"{eff_base}..HEAD")
            base_behind_origin[repo] = git_count(wt, f"{eff_base}..origin/{eff_base}")
        else:
            ahead_of_base[repo] = None
            base_behind_origin[repo] = None

    # Only the explicitly-active repo drives the scalar base_branch. Falling
    # back to the first inserted worktree would make a user-facing value depend
    # on dict order and could report one repo's base while the agent/UI is
    # focused on another (Greptile, MOS-229) — task.base_branch is the honest
    # task-level answer when no repo is active.
    active_repo = task.active_repo
    base_branch = (
        _effective_base_for_repo(task, active_repo, config)
        if active_repo is not None
        else task.base_branch
    )

    return {
        "slug": task.slug,
        "branch": task.branch,
        "base_branch": base_branch,
        "phase": task.phase,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "worktrees": {repo: str(p) for repo, p in task.worktrees.items()},
        "active_repo": task.active_repo,
        "ahead_of_origin": ahead_of_origin,
        "ahead_of_base": ahead_of_base,
        "base_behind_origin": base_behind_origin,
        "pr_urls": dict(task.pr_urls),
        "last_test_state": last_test_state,
        "last_test_iteration": last_test_iteration,
        "last_log_entry_at": _last_log_at(log_manager, task.slug),
        "drift": _drift_for_task(task.slug, cache),
    }


def build_context(
    state: WorkspaceState,
    config: WorkspaceConfig,
    log_manager: LogManager,
    cwd: Path,
    state_dir: Path,
    *,
    cache: Optional[ReconcileCache] = None,
    git_count: GitCounter = _git_count_default,
    dirty_check: DirtyCheck = _dirty_check_default,
    binary_check: Callable[[], Optional[bool]] = _binary_matches_editable_install,
    for_: Optional[str] = None,
    kind: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the agent-readable workspace context payload.

    `cache`, `git_count`, `dirty_check`, and `binary_check` are injection points
    for tests; production callers leave them at the defaults (with the CLI
    constructing a real `ReconcileCache(state_dir)`).

    `for_`/`kind` (MOS-100) shape the output for a specific audience -- see
    `build_audience_block()`. Validated up front (raising `AudienceError` on
    an invalid combination) before any of the git/log probing below, so a bad
    flag combo fails fast. Left at their defaults (both None), the returned
    payload is byte-for-byte identical to the pre-MOS-100 schema: no
    `audience` key at all.
    """
    audience = build_audience_block(for_, kind)

    active_tasks = [
        _task_payload(task, log_manager, git_count, cache, config)
        for task in state.tasks.values()
        if task.finished_at is None
    ]

    cwd_task, cwd_repo = _cwd_match(state, cwd)

    last_sync = read_last_sync_at(state_dir)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "active_tasks": active_tasks,
        "cwd_matches_task": cwd_task,
        "cwd_matches_repo": cwd_repo,
        "main_checkout_clean": _main_checkout_clean(config, dirty_check),
        "mship_binary_matches_editable_install": binary_check(),
        "last_workspace_fetch_at": last_sync.isoformat() if last_sync else None,
        "last_drift_check_at": _last_drift_check_at(cache),
    }
    if audience is not None:
        payload["audience"] = audience
    return payload
