"""Build the agent-agnostic subagent-prompt emitted by `mship dispatch`.

Pure builder — zero I/O, trivially unit-testable. The CLI wrapper in
src/mship/cli/dispatch.py handles resolution, subprocess calls, and stdout.

See docs/superpowers/specs/2026-04-17-mship-dispatch-design.md.
"""
from __future__ import annotations

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
