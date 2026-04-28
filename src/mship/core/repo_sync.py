"""Safe fast-forward reconciliation for repos that audit as behind-only."""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mship.core.config import WorkspaceConfig
from mship.core.diagnostics import capture_snapshot
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



def _detect_branch(root: Path, shell: ShellRunner) -> str | None:
    r = shell.run("git rev-parse --abbrev-ref HEAD", cwd=root)
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out if out and out != "HEAD" else None


def _try_recover_stale_main(
    repo: RepoAudit,
    cfg: WorkspaceConfig,
    shell: ShellRunner,
    state_dir: Path,
) -> tuple[bool, str]:
    """Attempt to recover from dirty-main-matches-upstream state.

    Returns (recovered, message). If recovered, the working tree has been
    reset so the caller's fast-forward path can run. If not recovered, no
    state mutation occurred — user's working tree untouched.
    """
    root = Path(_git_root_path(cfg, repo.name))
    # 1. Snapshot before doing anything.
    capture_snapshot(
        "sync", "dirty-main-pre-recovery", state_dir,
        repos={repo.name: root},
    )

    branch = repo.current_branch or _detect_branch(root, shell)
    if not branch:
        return (False, "could not resolve branch for recovery check")

    # 2. Fetch to ensure remote-tracking ref is current.
    fetch_r = shell.run("git fetch origin", cwd=root)
    if fetch_r.returncode != 0:
        return (False, f"fetch failed: {fetch_r.stderr.strip() or 'unknown'}")

    # 3. Behind check.
    r = shell.run(
        f"git rev-list --count {shlex.quote(branch)}..origin/{shlex.quote(branch)}",
        cwd=root,
    )
    if r.returncode != 0:
        return (False, f"behind-check failed: {r.stderr.strip()}")
    try:
        behind = int(r.stdout.strip() or "0")
    except ValueError:
        behind = 0
    if behind == 0:
        return (False, "not behind origin; not the recoverable pattern")

    # 4. Untracked check — recovery only handles the modified-tracked-files pattern.
    r = shell.run("git ls-files --others --exclude-standard", cwd=root)
    if r.returncode == 0 and r.stdout.strip():
        return (False, "untracked files present; recovery skipped to preserve data")

    # 5. Dirty tracked files enumeration.
    r = shell.run("git diff --name-only HEAD", cwd=root)
    if r.returncode != 0:
        return (False, f"diff --name-only failed: {r.stderr.strip()}")
    dirty_files = [p for p in r.stdout.splitlines() if p.strip()]
    if not dirty_files:
        return (False, "no dirty tracked files; nothing to recover")

    # 6. Per-file hash compare — PROVE redundancy BEFORE mutating state.
    mismatches: list[str] = []
    for path in dirty_files:
        wh = shell.run(f"git hash-object -- {shlex.quote(path)}", cwd=root)
        if wh.returncode != 0:
            mismatches.append(f"{path} (hash-object failed)")
            break
        working_hash = wh.stdout.strip()

        uh = shell.run(
            f"git rev-parse origin/{shlex.quote(branch)}:{shlex.quote(path)}",
            cwd=root,
        )
        if uh.returncode != 0:
            mismatches.append(path)
            break
        upstream_hash = uh.stdout.strip()

        if working_hash != upstream_hash:
            mismatches.append(path)
            break

    if mismatches:
        capture_snapshot(
            "sync", "dirty-main-real-user-work", state_dir,
            repos={repo.name: root},
            extra={"mismatched_files": mismatches},
        )
        return (
            False,
            f"dirty file {mismatches[0]} does not match upstream; real user work",
        )

    # 7. All files verified redundant — safe to reset.
    for path in dirty_files:
        r = shell.run(f"git checkout -- {shlex.quote(path)}", cwd=root)
        if r.returncode != 0:
            capture_snapshot(
                "sync", "dirty-main-reset-failed", state_dir,
                repos={repo.name: root},
                extra={"failed_path": path, "stderr": r.stderr},
            )
            return (
                False,
                f"checkout failed on {path} after hashes matched: {r.stderr.strip()}",
            )

    return (True, "recovered from stale main state")


def _result_for(
    repo: RepoAudit,
    cfg: WorkspaceConfig,
    shell: ShellRunner,
    state_dir: Path,
) -> SyncResult:
    blocking = [i for i in repo.issues if i.code in _BLOCKING_CODES]
    # Recovery attempt: only when dirty_worktree is the SOLE blocking code.
    if blocking and all(i.code == "dirty_worktree" for i in blocking):
        recovered, msg = _try_recover_stale_main(repo, cfg, shell, state_dir)
        if recovered:
            # Recovery just reset files but didn't pull — do that now.
            root = _git_root_path(cfg, repo.name)
            r = shell.run("git pull --ff-only", cwd=root)
            if r.returncode != 0:
                return SyncResult(
                    repo.name, repo.path, "skipped",
                    f"pull failed after recovery: {r.stderr.strip() or 'unknown'}",
                )
            return SyncResult(
                repo.name, repo.path, "fast_forwarded",
                "recovered from stale main state",
            )
        # Recovery declined → fall through to original skip.
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


def refresh_passive_worktrees(
    state_manager,
    config: WorkspaceConfig,
) -> list[SyncResult]:
    """Re-fetch and reset each passive worktree to `origin/<expected || base>`.

    Safe by construction: passive worktrees are detached HEAD, mship-managed,
    and pre-commit-hook-protected. Nothing the user could lose by hard reset.
    Returns a SyncResult per (task, repo) tuple for reporting.
    """
    import subprocess
    state = state_manager.load()
    results: list[SyncResult] = []
    for task in state.tasks.values():
        for repo_name in task.passive_repos:
            wt = task.worktrees.get(repo_name)
            if wt is None or not Path(wt).exists():
                continue
            repo_cfg = config.repos.get(repo_name)
            if repo_cfg is None:
                continue
            ref = repo_cfg.expected_branch or repo_cfg.base_branch
            if ref is None:
                results.append(SyncResult(
                    name=f"{task.slug}/{repo_name}",
                    path=Path(wt),
                    status="skipped",
                    message="no expected_branch or base_branch declared",
                ))
                continue
            canonical = repo_cfg.path
            fetch = subprocess.run(
                ["git", "fetch", "origin", ref], cwd=canonical,
                capture_output=True, text=True, check=False, timeout=60,
            )
            if fetch.returncode != 0:
                results.append(SyncResult(
                    name=f"{task.slug}/{repo_name}",
                    path=Path(wt),
                    status="skipped",
                    message=f"fetch failed: {fetch.stderr.strip()[:160]}",
                ))
                continue
            reset = subprocess.run(
                ["git", "-C", str(wt), "reset", "--hard", f"origin/{ref}"],
                capture_output=True, text=True, check=False,
            )
            if reset.returncode == 0:
                results.append(SyncResult(
                    name=f"{task.slug}/{repo_name}",
                    path=Path(wt),
                    status="fast_forwarded",
                    message=f"reset to origin/{ref}",
                ))
            else:
                results.append(SyncResult(
                    name=f"{task.slug}/{repo_name}",
                    path=Path(wt),
                    status="skipped",
                    message=f"reset failed: {reset.stderr.strip()[:160]}",
                ))
    return results


def sync_repos(
    report: AuditReport,
    config: WorkspaceConfig,
    shell: ShellRunner,
    state_dir: Path,
) -> SyncReport:
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
        results.append(_result_for(repo_audit, config, shell, state_dir))
    return SyncReport(results=tuple(results))
