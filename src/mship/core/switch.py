"""Build a cross-repo context-switch handoff for the agent."""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mship.core.config import WorkspaceConfig
from mship.core.log import LogEntry, LogManager
from mship.core.repo_state import audit_repos
from mship.core.state import WorkspaceState
from mship.util.shell import ShellRunner


@dataclass(frozen=True)
class DepChange:
    repo: str
    commit_count: int
    commits: tuple[str, ...]
    files_changed: tuple[str, ...]
    additions: int
    deletions: int
    error: str | None = None


@dataclass(frozen=True)
class Handoff:
    repo: str
    task_slug: str
    phase: str
    branch: str
    worktree_path: Path
    worktree_missing: bool
    finished_at: datetime | None
    dep_changes: tuple[DepChange, ...]
    last_log_in_repo: LogEntry | None
    drift_error_count: int
    test_status: str | None

    def to_json(self) -> dict:
        return {
            "repo": self.repo,
            "task_slug": self.task_slug,
            "phase": self.phase,
            "branch": self.branch,
            "worktree_path": str(self.worktree_path),
            "worktree_missing": self.worktree_missing,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "dep_changes": [
                {
                    "repo": d.repo,
                    "commit_count": d.commit_count,
                    "commits": list(d.commits),
                    "files_changed": list(d.files_changed),
                    "additions": d.additions,
                    "deletions": d.deletions,
                    "error": d.error,
                }
                for d in self.dep_changes
            ],
            "last_log_in_repo": (
                {
                    "timestamp": self.last_log_in_repo.timestamp.isoformat(),
                    "message": self.last_log_in_repo.message,
                }
                if self.last_log_in_repo is not None else None
            ),
            "drift_error_count": self.drift_error_count,
            "test_status": self.test_status,
        }


def _run(shell: ShellRunner, cmd: str, cwd: Path) -> tuple[int, str, str]:
    r = shell.run(cmd, cwd=cwd)
    return r.returncode, r.stdout, r.stderr


def _fallback_anchor(
    shell: ShellRunner,
    dep_worktree: Path,
    task_branch: str,
    dep_config,
    cli_fallback: str | None,
) -> str | None:
    """First-switch anchor: merge-base of task branch with the dep's base branch.

    Order: configured base_branch, origin/HEAD symbolic ref, origin/main, main, None.
    """
    base = dep_config.base_branch if dep_config is not None and dep_config.base_branch is not None else None
    candidates: list[str] = []
    if base is not None:
        candidates.append(base)
        candidates.append(f"origin/{base}")
    candidates.append("origin/HEAD")
    candidates.append("origin/main")
    candidates.append("main")
    for ref in candidates:
        rc, out, _ = _run(
            shell,
            f"git merge-base {shlex.quote(ref)} {shlex.quote(task_branch)}",
            dep_worktree,
        )
        if rc == 0 and out.strip():
            return out.strip()
    return None


def _collect_dep_change(
    shell: ShellRunner,
    dep_name: str,
    dep_worktree: Path | None,
    anchor_sha: str | None,
    task_branch: str,
    dep_config,
) -> DepChange | None:
    """Return DepChange for a dep, or None if no changes to report."""
    if dep_worktree is None or not dep_worktree.exists():
        return DepChange(
            repo=dep_name, commit_count=0, commits=(), files_changed=(),
            additions=0, deletions=0, error="worktree unavailable",
        )
    if anchor_sha is None:
        anchor_sha = _fallback_anchor(shell, dep_worktree, task_branch, dep_config, None)
        if anchor_sha is None:
            return DepChange(
                repo=dep_name, commit_count=0, commits=(), files_changed=(),
                additions=0, deletions=0, error="no merge-base for task branch",
            )

    spec = f"{anchor_sha}..HEAD"
    rc, out, err = _run(shell, f"git log --format=%h\\ %s {shlex.quote(spec)}", dep_worktree)
    if rc != 0:
        return DepChange(
            repo=dep_name, commit_count=0, commits=(), files_changed=(),
            additions=0, deletions=0,
            error=(err.strip().splitlines()[-1] if err.strip() else "git log failed"),
        )
    commits = tuple(line for line in out.splitlines() if line)
    if not commits:
        return None

    rc2, out2, _ = _run(shell, f"git diff --numstat {shlex.quote(spec)}", dep_worktree)
    files: list[str] = []
    additions = 0
    deletions = 0
    if rc2 == 0:
        for line in out2.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                a_s, d_s, path = parts[0], parts[1], parts[2]
                try:
                    additions += int(a_s)
                except ValueError:
                    pass
                try:
                    deletions += int(d_s)
                except ValueError:
                    pass
                files.append(path)

    return DepChange(
        repo=dep_name,
        commit_count=len(commits),
        commits=commits,
        files_changed=tuple(files),
        additions=additions,
        deletions=deletions,
    )


def build_handoff(
    config: WorkspaceConfig,
    state: WorkspaceState,
    shell: ShellRunner,
    log_manager: LogManager,
    repo: str,
) -> Handoff:
    assert state.current_task is not None
    task = state.tasks[state.current_task]

    worktree_path = Path(task.worktrees.get(repo, config.repos[repo].path))
    worktree_missing = not worktree_path.exists()

    # Deps
    repo_cfg = config.repos[repo]
    stored = task.last_switched_at_sha.get(repo, {})
    dep_changes: list[DepChange] = []
    for dep in repo_cfg.depends_on:
        dep_name = dep.repo
        dep_worktree = Path(task.worktrees[dep_name]) if dep_name in task.worktrees else None
        anchor = stored.get(dep_name)
        dep_cfg = config.repos.get(dep_name)
        change = _collect_dep_change(
            shell, dep_name, dep_worktree, anchor, task.branch, dep_cfg,
        )
        if change is not None:
            dep_changes.append(change)

    # Last log
    last_log = None
    try:
        entries = log_manager.read(task.slug, last=1)
        if entries:
            last_log = entries[-1]
    except Exception:
        last_log = None

    # Drift (local-only, scoped to target repo)
    drift_error_count = 0
    try:
        report = audit_repos(config, shell, names=[repo], local_only=True)
        drift_error_count = sum(
            1 for r in report.repos for i in r.issues if i.severity == "error"
        )
    except Exception:
        drift_error_count = 0

    # Test status
    test = task.test_results.get(repo)
    test_status = test.status if test is not None else None

    return Handoff(
        repo=repo,
        task_slug=task.slug,
        phase=task.phase,
        branch=task.branch,
        worktree_path=worktree_path,
        worktree_missing=worktree_missing,
        finished_at=task.finished_at,
        dep_changes=tuple(dep_changes),
        last_log_in_repo=last_log,
        drift_error_count=drift_error_count,
        test_status=test_status,
    )
