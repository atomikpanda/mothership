"""Repo drift detection — data model and audit entry point."""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

Severity = Literal["error", "info"]


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str


@dataclass(frozen=True)
class RepoAudit:
    name: str
    path: Path
    current_branch: str | None
    issues: tuple[Issue, ...]

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


@dataclass(frozen=True)
class AuditReport:
    repos: tuple[RepoAudit, ...]

    @property
    def has_errors(self) -> bool:
        return any(r.has_errors for r in self.repos)

    def to_json(self, workspace: str) -> dict:
        return {
            "workspace": workspace,
            "has_errors": self.has_errors,
            "repos": [
                {
                    "name": r.name,
                    "path": str(r.path),
                    "current_branch": r.current_branch,
                    "issues": [
                        {"code": i.code, "severity": i.severity, "message": i.message}
                        for i in r.issues
                    ],
                }
                for r in self.repos
            ],
        }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _effective_path(cfg, name: str) -> Path:
    repo = cfg.repos[name]
    if repo.git_root is not None:
        parent = cfg.repos[repo.git_root]
        return (parent.path / repo.path).resolve()
    return repo.path


def _git_root_path(cfg, name: str) -> Path:
    repo = cfg.repos[name]
    if repo.git_root is not None:
        return cfg.repos[repo.git_root].path
    return repo.path


def _git_root_key(cfg, name: str) -> str:
    """The name of the repo that owns the git root this repo belongs to."""
    repo = cfg.repos[name]
    return repo.git_root if repo.git_root is not None else name


def _sh_out(shell, cmd: str, cwd: Path) -> tuple[int, str, str]:
    r = shell.run(cmd, cwd=cwd)
    return r.returncode, r.stdout, r.stderr


def _list_worktree_paths(shell, root_path: Path) -> list[Path]:
    """Parse `git worktree list --porcelain` into resolved absolute Paths."""
    rc, out, _ = _sh_out(shell, "git worktree list --porcelain", root_path)
    if rc != 0:
        return []
    paths: list[Path] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree "):]).resolve())
    return paths


def _probe_git_wide(
    shell,
    root_path: Path,
    expected_branch: str | None,
    allow_extra_worktrees: bool,
    known_worktree_paths: frozenset[Path],
) -> tuple[str | None, list[Issue]]:
    """Run checks that operate on the git root. Returns (current_branch, issues)."""
    issues: list[Issue] = []

    # Branch / detached
    rc, out, _ = _sh_out(shell, "git symbolic-ref --short HEAD", root_path)
    if rc != 0:
        issues.append(Issue("detached_head", "error", "HEAD is detached"))
        current_branch = None
    else:
        current_branch = out.strip()
        if expected_branch is not None and current_branch != expected_branch:
            issues.append(Issue(
                "unexpected_branch", "error",
                f"on {current_branch!r}, expected {expected_branch!r}",
            ))

    # Fetch (needed for ahead/behind)
    rc, _, err = _sh_out(shell, "git fetch --prune origin", root_path)
    fetch_ok = rc == 0
    if not fetch_ok:
        issues.append(Issue(
            "fetch_failed", "error",
            err.strip().splitlines()[-1] if err.strip() else "fetch failed",
        ))

    # Upstream tracking
    if current_branch is not None and fetch_ok:
        rc, _, _ = _sh_out(
            shell, "git rev-parse --abbrev-ref --symbolic-full-name @{u}", root_path
        )
        if rc != 0:
            issues.append(Issue(
                "no_upstream", "error", "current branch has no tracking remote"
            ))
        else:
            rc_ah, out_ah, _ = _sh_out(shell, "git rev-list --count @{u}..HEAD", root_path)
            rc_be, out_be, _ = _sh_out(shell, "git rev-list --count HEAD..@{u}", root_path)
            if rc_ah == 0 and rc_be == 0:
                ahead = int(out_ah.strip() or "0")
                behind = int(out_be.strip() or "0")
                if ahead and behind:
                    issues.append(Issue(
                        "diverged", "error",
                        f"{ahead} ahead, {behind} behind origin",
                    ))
                elif behind:
                    issues.append(Issue(
                        "behind_remote", "error",
                        f"behind origin by {behind} commits",
                    ))
                elif ahead:
                    issues.append(Issue(
                        "ahead_remote", "info",
                        f"ahead of origin by {ahead} commits",
                    ))

    # Extra worktrees — exclude ones mship knows about.
    if not allow_extra_worktrees:
        wt_paths = _list_worktree_paths(shell, root_path)
        unknown = [p for p in wt_paths if p not in known_worktree_paths]
        if len(unknown) > 1:
            issues.append(Issue(
                "extra_worktrees", "error",
                f"{len(unknown) - 1} worktree(s) at paths mship doesn't track "
                "(run `mship prune` to list/clean orphans, or check for foreign worktrees)",
            ))

    return current_branch, issues


def _probe_dirty(
    shell,
    root_path: Path,
    subdir: Path | None,
    allow_dirty: bool,
) -> Issue | None:
    if allow_dirty:
        return None
    cmd = "git status --porcelain"
    if subdir is not None:
        cmd += f" -- {shlex.quote(str(subdir))}"
    rc, out, _ = _sh_out(shell, cmd, root_path)
    if rc != 0:
        return None
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if lines:
        return Issue(
            "dirty_worktree", "error",
            f"{len(lines)} uncommitted change" + ("s" if len(lines) != 1 else ""),
        )
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def audit_repos(
    config,
    shell,
    names: Iterable[str] | None = None,
    known_worktree_paths: frozenset[Path] = frozenset(),
) -> AuditReport:
    """Run drift audit across repos, grouping by git root for git-wide checks."""
    target_names = list(names) if names is not None else list(config.repos.keys())
    unknown = [n for n in target_names if n not in config.repos]
    if unknown:
        raise ValueError(f"unknown repo(s): {', '.join(sorted(unknown))}")

    # Group by effective git root
    groups: dict[str, list[str]] = {}
    for name in target_names:
        groups.setdefault(_git_root_key(config, name), []).append(name)

    per_repo_issues: dict[str, list[Issue]] = {n: [] for n in target_names}
    per_repo_branch: dict[str, str | None] = {n: None for n in target_names}

    for root_key, members in groups.items():
        root_path = _git_root_path(config, root_key)

        if not root_path.exists():
            for m in members:
                per_repo_issues[m].append(Issue(
                    "path_missing", "error", f"path not found: {root_path}",
                ))
            continue
        if not (root_path / ".git").exists():
            for m in members:
                per_repo_issues[m].append(Issue(
                    "not_a_git_repo", "error", f"no .git at {root_path}",
                ))
            continue

        # Pick expected_branch / allow_extra_worktrees from the root's own RepoConfig,
        # falling back to any member's declaration (validator guarantees consistency).
        root_cfg = config.repos.get(root_key)
        expected = root_cfg.expected_branch if root_cfg is not None else None
        if expected is None:
            for m in members:
                if config.repos[m].expected_branch is not None:
                    expected = config.repos[m].expected_branch
                    break
        allow_wt = any(config.repos[m].allow_extra_worktrees for m in members) or (
            root_cfg.allow_extra_worktrees if root_cfg is not None else False
        )

        current_branch, wide_issues = _probe_git_wide(
            shell, root_path, expected, allow_wt, known_worktree_paths,
        )
        for m in members:
            per_repo_branch[m] = current_branch
            per_repo_issues[m].extend(wide_issues)

        # Per-repo dirty check, scoped to subdir when applicable.
        for m in members:
            m_cfg = config.repos[m]
            subdir: Path | None = None
            if m_cfg.git_root is not None:
                subdir = m_cfg.path  # relative path within the parent
            di = _probe_dirty(shell, root_path, subdir, m_cfg.allow_dirty)
            if di is not None:
                per_repo_issues[m].append(di)

    repos = tuple(
        RepoAudit(
            name=n,
            path=_effective_path(config, n),
            current_branch=per_repo_branch[n],
            issues=tuple(per_repo_issues[n]),
        )
        for n in target_names
    )
    return AuditReport(repos=repos)
