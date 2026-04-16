"""Fetch upstream PR snapshots and local git-drift snapshots.

- `fetch_pr_snapshots(branches)` -> dict[branch, PRSnapshot] via one batched `gh pr list`.
- `collect_git_snapshots(worktrees_by_branch, runner)` -> dict[branch, GitSnapshot].
- Offline / gh-missing / non-zero-exit -> raises FetchError; callers decide fallback.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from mship.core.reconcile.detect import GitSnapshot, PRSnapshot


class FetchError(RuntimeError):
    pass


# --- gh PR fetching ---------------------------------------------------------


def gh_search_query(branches: list[str]) -> str:
    """Build a `head:` search string for `gh pr list --search`."""
    return " ".join(f"head:{b}" for b in branches)


def parse_gh_pr_list(raw: list[dict]) -> dict[str, PRSnapshot]:
    """Map gh's JSON array to {head_ref: PRSnapshot}, most-recent wins on dupes."""
    out: dict[str, PRSnapshot] = {}
    for entry in raw:
        try:
            head = entry["headRefName"]
            state = entry["state"]
            base = entry["baseRefName"]
            url = entry["url"]
            updated = entry["updatedAt"]
        except (KeyError, TypeError):
            continue
        merge = None
        mc = entry.get("mergeCommit")
        if isinstance(mc, dict):
            merge = mc.get("oid")
        snap = PRSnapshot(
            head_ref=head, state=state, base_ref=base,
            merge_commit=merge, url=url, updated_at=updated,
        )
        prev = out.get(head)
        if prev is None or snap.updated_at > prev.updated_at:
            out[head] = snap
    return out


def fetch_pr_snapshots(branches: list[str], *, timeout: int = 30) -> dict[str, PRSnapshot]:
    if not branches:
        return {}
    if shutil.which("gh") is None:
        raise FetchError("gh CLI not installed")
    query = gh_search_query(branches)
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--search", query,
             "--json", "headRefName,state,baseRefName,mergeCommit,url,updatedAt",
             "--limit", "100"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise FetchError(f"gh invocation failed: {e!r}") from e
    if result.returncode != 0:
        raise FetchError(f"gh exit {result.returncode}: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        raise FetchError(f"gh returned invalid JSON: {e!r}") from e
    if not isinstance(data, list):
        raise FetchError("gh returned non-list payload")
    return parse_gh_pr_list(data)


# --- git local snapshot -----------------------------------------------------


class GitRunner(Protocol):
    def run(self, args: list[str], cwd: Path | None = None) -> tuple[int, str]: ...


class _SubprocessGit:
    def run(self, args: list[str], cwd: Path | None = None) -> tuple[int, str]:
        try:
            r = subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=15,
                cwd=str(cwd) if cwd else None,
            )
        except (subprocess.SubprocessError, OSError) as e:
            return (1, repr(e))
        return (r.returncode, r.stdout)


def collect_git_snapshots(
    worktrees_by_branch: dict[str, Path],
    *,
    runner: GitRunner | None = None,
) -> dict[str, GitSnapshot]:
    """For each (branch, worktree-path), compute behind/ahead via rev-list."""
    runner = runner or _SubprocessGit()
    out: dict[str, GitSnapshot] = {}
    for branch, wt_path in worktrees_by_branch.items():
        rc, _ = runner.run(["rev-parse", "--abbrev-ref", f"{branch}@{{u}}"], cwd=wt_path)
        if rc != 0:
            out[branch] = GitSnapshot(has_upstream=False, behind=0, ahead=0)
            continue
        rc, stdout = runner.run(
            ["rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd=wt_path,
        )
        if rc != 0:
            out[branch] = GitSnapshot(has_upstream=True, behind=0, ahead=0)
            continue
        parts = stdout.strip().split()
        try:
            behind, ahead = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            behind, ahead = 0, 0
        out[branch] = GitSnapshot(has_upstream=True, behind=behind, ahead=ahead)
    return out


# --- workspace default branch helper ---------------------------------------


def workspace_default_branch(container) -> str | None:
    """Return the workspace's main repo's default branch name, or None on error."""
    if shutil.which("gh") is None:
        return None
    try:
        repos = list(container.config().repos.keys())
    except Exception:
        return None
    if not repos:
        return None
    try:
        result = subprocess.run(
            ["gh", "repo", "view", repos[0], "--json", "defaultBranchRef"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
        return data["defaultBranchRef"]["name"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def workspace_default_branch_from_config(config) -> str | None:
    """Like workspace_default_branch, but takes a WorkspaceConfig directly."""
    if shutil.which("gh") is None:
        return None
    repos = list(getattr(config, "repos", {}).keys())
    if not repos:
        return None
    try:
        result = subprocess.run(
            ["gh", "repo", "view", repos[0], "--json", "defaultBranchRef"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
        return data["defaultBranchRef"]["name"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
