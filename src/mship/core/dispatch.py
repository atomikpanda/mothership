"""Build the agent-agnostic subagent-prompt emitted by `mship dispatch`.

Pure builder — zero I/O, trivially unit-testable. The CLI wrapper in
src/mship/cli/dispatch.py handles resolution, subprocess calls, and stdout.

See docs/superpowers/specs/2026-04-17-mship-dispatch-design.md.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from mship.core.state import Task


_CANONICAL_SKILL_NAMES: tuple[str, ...] = (
    "working-with-mothership",
    "test-driven-development",
    "finishing-a-development-branch",
    "verification-before-completion",
)


@dataclass(frozen=True)
class SkillRef:
    name: str
    path: Path


def canonical_skills(pkg_skills_source: Path) -> list[SkillRef]:
    """Return the four canonical skills every dispatched subagent should read."""
    return [
        SkillRef(name=n, path=pkg_skills_source / n / "SKILL.md")
        for n in _CANONICAL_SKILL_NAMES
    ]


def resolve_repo(task: Task, repo_flag: str | None) -> str:
    """Pick which repo's worktree the dispatch prompt targets.

    Priority: --repo flag > task.active_repo > sole worktree > ValueError.
    """
    if repo_flag is not None:
        if repo_flag not in task.worktrees:
            raise ValueError(
                f"unknown repo: {repo_flag!r}. "
                f"Task affects: {sorted(task.worktrees)}"
            )
        return repo_flag
    if task.active_repo and task.active_repo in task.worktrees:
        return task.active_repo
    if len(task.worktrees) == 1:
        return next(iter(task.worktrees))
    raise ValueError(
        f"task {task.slug!r} affects {len(task.worktrees)} repos and no "
        f"active_repo is set; pass --repo <name> or run mship switch <repo> "
        f"first. Affected repos: {sorted(task.worktrees)}"
    )


@dataclass(frozen=True)
class BaseShaInfo:
    base_sha: str | None         # local <base_branch>
    origin_base_sha: str | None  # remote origin/<base_branch>
    head_sha: str                # current HEAD of the worktree
    ahead_of_base: int | None
    base_behind_origin: int | None
    has_upstream: bool
    summary: str                 # one-line human-readable


def _git_out(args: list[str], cwd: Path, timeout: int = 10) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def collect_base_sha_info(worktree: Path, base_branch: str) -> BaseShaInfo:
    """Probe local `<base>`, `origin/<base>`, and HEAD. Graceful on missing upstream."""
    head_sha = _git_out(["rev-parse", "--short", "HEAD"], cwd=worktree) or "?"
    base_sha = _git_out(["rev-parse", "--short", base_branch], cwd=worktree)  # spec: always local
    origin_base_sha = _git_out(
        ["rev-parse", "--short", f"origin/{base_branch}"], cwd=worktree,
    )
    has_upstream = origin_base_sha is not None

    ahead_of_base: int | None = None
    base_behind_origin: int | None = None
    if base_sha:
        out = _git_out(["rev-list", "--count", f"{base_branch}..HEAD"], cwd=worktree)
        try:
            ahead_of_base = int(out) if out is not None else None
        except ValueError:
            ahead_of_base = None
    if base_sha and has_upstream:
        out = _git_out(
            ["rev-list", "--count", f"{base_branch}..origin/{base_branch}"],
            cwd=worktree,
        )
        try:
            base_behind_origin = int(out) if out is not None else None
        except ValueError:
            base_behind_origin = None

    summary = _summarize_base_sha(
        ahead_of_base=ahead_of_base,
        base_behind_origin=base_behind_origin,
        has_upstream=has_upstream,
        base_branch=base_branch,
    )
    return BaseShaInfo(
        base_sha=base_sha, origin_base_sha=origin_base_sha, head_sha=head_sha,
        ahead_of_base=ahead_of_base, base_behind_origin=base_behind_origin,
        has_upstream=has_upstream, summary=summary,
    )


def _summarize_base_sha(
    *, ahead_of_base: int | None, base_behind_origin: int | None,
    has_upstream: bool, base_branch: str,
) -> str:
    parts = []
    if not has_upstream:
        parts.append(f"no upstream tracked for `{base_branch}`")
    elif base_behind_origin == 0:
        parts.append(f"base is in sync with origin")
    elif base_behind_origin and base_behind_origin > 0:
        plural = "s" if base_behind_origin != 1 else ""
        parts.append(f"base is {base_behind_origin} commit{plural} behind origin")
    if ahead_of_base is not None:
        plural = "s" if ahead_of_base != 1 else ""
        if ahead_of_base == 0:
            parts.append(f"HEAD is at base")
        else:
            parts.append(f"HEAD is {ahead_of_base} commit{plural} ahead of base")
    return "; ".join(parts) if parts else "unknown"
