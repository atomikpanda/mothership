"""Pure detection: given local task state + a snapshot of upstream + local git,
compute an UpstreamState for each task. No I/O."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class UpstreamState(str, Enum):
    in_sync = "in_sync"
    merged = "merged"
    closed = "closed"
    diverged = "diverged"
    base_changed = "base_changed"
    missing = "missing"


@dataclass(frozen=True)
class PRSnapshot:
    head_ref: str
    state: str                 # "OPEN" | "CLOSED" | "MERGED" | "DRAFT"
    base_ref: str
    merge_commit: str | None
    url: str
    updated_at: str


@dataclass(frozen=True)
class GitSnapshot:
    has_upstream: bool
    behind: int                # remote-only commits
    ahead: int                 # local-only commits


@dataclass(frozen=True)
class Detection:
    state: UpstreamState
    pr_url: str | None
    pr_number: int | None
    base: str | None
    merge_commit: str | None
    updated_at: str | None


def _pr_number(url: str | None) -> int | None:
    if not url:
        return None
    try:
        return int(url.rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


def detect_one(
    task_branch: str,
    task_base: str | None,
    pr: PRSnapshot | None,
    git: GitSnapshot,
) -> Detection:
    if pr is None:
        return Detection(
            state=UpstreamState.missing,
            pr_url=None, pr_number=None, base=None,
            merge_commit=None, updated_at=None,
        )
    if pr.state == "MERGED":
        return Detection(
            state=UpstreamState.merged,
            pr_url=pr.url, pr_number=_pr_number(pr.url), base=pr.base_ref,
            merge_commit=pr.merge_commit, updated_at=pr.updated_at,
        )
    if pr.state == "CLOSED":
        return Detection(
            state=UpstreamState.closed,
            pr_url=pr.url, pr_number=_pr_number(pr.url), base=pr.base_ref,
            merge_commit=None, updated_at=pr.updated_at,
        )
    if task_base is not None and pr.base_ref != task_base:
        return Detection(
            state=UpstreamState.base_changed,
            pr_url=pr.url, pr_number=_pr_number(pr.url), base=pr.base_ref,
            merge_commit=None, updated_at=pr.updated_at,
        )
    if git.has_upstream and git.behind > 0:
        return Detection(
            state=UpstreamState.diverged,
            pr_url=pr.url, pr_number=_pr_number(pr.url), base=pr.base_ref,
            merge_commit=None, updated_at=pr.updated_at,
        )
    return Detection(
        state=UpstreamState.in_sync,
        pr_url=pr.url, pr_number=_pr_number(pr.url), base=pr.base_ref,
        merge_commit=None, updated_at=pr.updated_at,
    )


def detect_many(
    tasks: Sequence[tuple[str, str, str | None]],   # (slug, branch, base)
    pr_by_head: dict[str, PRSnapshot],
    git_by_branch: dict[str, GitSnapshot],
) -> dict[str, Detection]:
    out: dict[str, Detection] = {}
    for slug, branch, base in tasks:
        pr = pr_by_head.get(branch)
        git = git_by_branch.get(branch, GitSnapshot(has_upstream=False, behind=0, ahead=0))
        out[slug] = detect_one(branch, base, pr, git)
    return out
