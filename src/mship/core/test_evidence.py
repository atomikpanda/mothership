"""Unified reader for test-run evidence on an mship task.

Consumers (`mship finish`, `mship phase`) ask "did we actually test this?"
before opening PRs or transitioning phases. This module answers by folding
two signal sources into a per-repo status:

1. `task.test_results[<repo>]` populated by `mship test`. Strongest.
2. Journal entries with `test_state` populated by `mship journal --test-state`.
   Per-repo entries beat global (no-repo) entries; within each scope the
   most recent wins.

When a shell + per-repo paths are provided, `passed` evidence older than the
latest commit on the task branch is demoted to `stale` — the user may have
committed more work since running tests.

See #81 for the issue + design rationale. No automatic test invocation:
this module reads evidence, it never creates it.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from mship.core.log import LogManager
from mship.core.state import Task
from mship.util.shell import ShellRunner


EvidenceStatus = Literal["passed", "failed", "missing", "stale"]


@dataclass(frozen=True)
class RepoEvidence:
    status: EvidenceStatus
    source: Literal["test_results", "journal", "none"]
    at: datetime | None


_TEST_STATE_TO_STATUS: dict[str, EvidenceStatus] = {
    "pass": "passed",
    "fail": "failed",
    "mixed": "failed",
}


def read_evidence(
    task: Task,
    log: LogManager,
    *,
    shell: ShellRunner | None = None,
    repo_paths: dict[str, Path] | None = None,
) -> dict[str, RepoEvidence]:
    """Return per-repo test-run evidence for the task's affected repos.

    Arguments:
        task: the task whose affected_repos + test_results we inspect.
        log:  the LogManager to read journal entries from.
        shell: optional ShellRunner. When provided with `repo_paths`, the
               reader upgrades stale `passed` evidence (evidence older than
               the latest commit on the task branch) to `stale`.
        repo_paths: optional {repo: path} used with `shell` for the stale
                    check. Path is where `git log` should run for that repo.
    """
    global_latest, per_repo_latest = _scan_journal(task, log)
    results: dict[str, RepoEvidence] = {}
    for repo in task.affected_repos:
        ev = _resolve_repo(repo, task, global_latest, per_repo_latest)
        if (
            ev.status == "passed"
            and ev.at is not None
            and shell is not None
            and repo_paths is not None
        ):
            head_ts = _head_commit_time(shell, repo_paths.get(repo), task.branch)
            if head_ts is not None and head_ts > ev.at:
                ev = RepoEvidence(status="stale", source=ev.source, at=ev.at)
        results[repo] = ev
    return results


def _scan_journal(
    task: Task, log: LogManager,
) -> tuple[tuple[datetime, str] | None, dict[str, tuple[datetime, str]]]:
    global_latest: tuple[datetime, str] | None = None
    per_repo_latest: dict[str, tuple[datetime, str]] = {}
    # Iterate in file/chronological order; later entries win ties at the
    # second-precision timestamp the journal stores.
    for e in log.read(task.slug):
        if e.test_state is None:
            continue
        if e.repo is None:
            if global_latest is None or e.timestamp >= global_latest[0]:
                global_latest = (e.timestamp, e.test_state)
        else:
            existing = per_repo_latest.get(e.repo)
            if existing is None or e.timestamp >= existing[0]:
                per_repo_latest[e.repo] = (e.timestamp, e.test_state)
    return global_latest, per_repo_latest


def _resolve_repo(
    repo: str,
    task: Task,
    global_latest: tuple[datetime, str] | None,
    per_repo_latest: dict[str, tuple[datetime, str]],
) -> RepoEvidence:
    tr = task.test_results.get(repo)
    if tr is not None:
        status: EvidenceStatus = (
            "passed" if tr.status == "pass"
            else "failed" if tr.status == "fail"
            else "missing"
        )
        return RepoEvidence(status=status, source="test_results", at=tr.at)
    if repo in per_repo_latest:
        at, state = per_repo_latest[repo]
        status = _TEST_STATE_TO_STATUS.get(state, "missing")
        return RepoEvidence(status=status, source="journal", at=at)
    if global_latest is not None:
        at, state = global_latest
        status = _TEST_STATE_TO_STATUS.get(state, "missing")
        return RepoEvidence(status=status, source="journal", at=at)
    return RepoEvidence(status="missing", source="none", at=None)


def _head_commit_time(
    shell: ShellRunner, repo_path: Path | None, branch: str,
) -> datetime | None:
    if repo_path is None:
        return None
    r = shell.run(
        f"git log -1 --format=%cI {shlex.quote(branch)}",
        cwd=repo_path,
    )
    if r.returncode != 0:
        return None
    raw = r.stdout.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def format_missing_summary(
    evidence: dict[str, RepoEvidence],
) -> list[str]:
    """Human-readable warning lines for non-passed repos, grouped by status.

    Returns an empty list when every repo has passing, non-stale evidence.
    """
    missing = [r for r, e in evidence.items() if e.status == "missing"]
    stale = [r for r, e in evidence.items() if e.status == "stale"]
    failing = [r for r, e in evidence.items() if e.status == "failed"]
    lines: list[str] = []
    if missing:
        lines.append(f"Tests not run in: {', '.join(sorted(missing))}")
    if stale:
        lines.append(
            f"Tests stale (branch has new commits) in: {', '.join(sorted(stale))}"
        )
    if failing:
        lines.append(f"Tests failing in: {', '.join(sorted(failing))}")
    return lines
