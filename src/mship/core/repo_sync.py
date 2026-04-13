"""Safe fast-forward reconciliation for repos that audit as behind-only."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mship.core.config import WorkspaceConfig
from mship.core.repo_state import AuditReport, RepoAudit
from mship.util.shell import ShellRunner


Status = Literal["up_to_date", "fast_forwarded", "skipped"]


@dataclass(frozen=True)
class SyncResult:
    name: str
    path: Path
    status: Status
    message: str


@dataclass(frozen=True)
class SyncReport:
    results: tuple[SyncResult, ...]

    @property
    def has_errors(self) -> bool:
        return any(r.status == "skipped" for r in self.results)


_BLOCKING_CODES = {
    "path_missing", "not_a_git_repo", "fetch_failed", "detached_head",
    "unexpected_branch", "dirty_worktree", "no_upstream", "diverged",
}


def _git_root_path(cfg: WorkspaceConfig, name: str) -> Path:
    repo = cfg.repos[name]
    if repo.git_root is not None:
        return cfg.repos[repo.git_root].path
    return repo.path


def _result_for(repo: RepoAudit, cfg: WorkspaceConfig, shell: ShellRunner) -> SyncResult:
    blocking = [i for i in repo.issues if i.code in _BLOCKING_CODES]
    if blocking:
        first = blocking[0]
        return SyncResult(repo.name, repo.path, "skipped",
                          f"{first.code} — {first.message}")
    behind = [i for i in repo.issues if i.code == "behind_remote"]
    if behind:
        root = _git_root_path(cfg, repo.name)
        r = shell.run("git pull --ff-only", cwd=root)
        if r.returncode != 0:
            return SyncResult(repo.name, repo.path, "skipped",
                              f"pull failed: {r.stderr.strip() or 'unknown error'}")
        # Extract the commit count reported in audit issue message (e.g. "behind origin by 3 commits")
        msg = behind[0].message
        return SyncResult(repo.name, repo.path, "fast_forwarded", msg)
    return SyncResult(repo.name, repo.path, "up_to_date", "no action")


def sync_repos(report: AuditReport, config: WorkspaceConfig, shell: ShellRunner) -> SyncReport:
    # Avoid double fast-forwarding subdir repos that share a git root.
    seen_roots: set[str] = set()
    results: list[SyncResult] = []
    for repo_audit in report.repos:
        root_key = config.repos[repo_audit.name].git_root or repo_audit.name
        if root_key in seen_roots:
            # Mirror whatever result the root already got, minus the pull.
            prev = next(r for r in results if
                        (config.repos[r.name].git_root or r.name) == root_key)
            results.append(SyncResult(
                repo_audit.name, repo_audit.path, prev.status,
                f"(shared git root with {prev.name}) {prev.message}",
            ))
            continue
        seen_roots.add(root_key)
        results.append(_result_for(repo_audit, config, shell))
    return SyncReport(results=tuple(results))
