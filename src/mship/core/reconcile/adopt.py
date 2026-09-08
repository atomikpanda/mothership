"""Explicit, fail-closed recovery of missing metadata for merged task PRs."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mship.core.base_resolver import resolve_base
from mship.core.log import LogManager
from mship.core.pr import PRManager
from mship.core.reconcile.cache import ReconcileCache
from mship.core.state import StateManager, Task
from mship.util.shell import ShellRunner


class AdoptionError(RuntimeError):
    """A requested merged-PR metadata recovery could not be proven safe."""


_GITHUB_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True)
class VerifiedMergedPR:
    repo: str
    url: str
    merge_commit: str
    merged_at: datetime


def adopt_merged_task(
    task_slug: str,
    *,
    state_manager: StateManager,
    config: Any,
    shell: ShellRunner,
    pr_manager: PRManager,
    cache: ReconcileCache,
    log: LogManager,
) -> tuple[bool, list[VerifiedMergedPR]]:
    """Recover one task's PR URLs and finish time after proving all deliveries.

    GitHub and git checks are deliberately complete before the one state
    mutation.  The mutation compares the task with the snapshot used for those
    checks, so a concurrent task change cannot be overwritten.
    """
    state = state_manager.load()
    task = state.tasks.get(task_slug)
    if task is None:
        raise AdoptionError(f"Unknown task: {task_slug!r}.")
    expected = task.model_dump(mode="json")
    if not task.affected_repos:
        raise AdoptionError(f"Task {task_slug!r} has no affected repositories.")

    try:
        pr_manager.check_gh_available()
    except RuntimeError as exc:
        raise AdoptionError(str(exc)) from exc

    verified = [
        _verify_repo(task, repo, config, shell, pr_manager)
        for repo in task.affected_repos
    ]
    recovered_urls = {entry.repo: entry.url for entry in verified}
    completed_at = max(entry.merged_at for entry in verified)

    changed = False

    def apply(current_state) -> None:
        nonlocal changed
        current = current_state.tasks.get(task_slug)
        if current is None or current.model_dump(mode="json") != expected:
            raise AdoptionError(
                f"Task {task_slug!r} changed while PR recovery was being verified; retry it."
            )
        for repo, url in recovered_urls.items():
            recorded = current.pr_urls.get(repo)
            if recorded is not None and recorded != url:
                raise AdoptionError(
                    f"{repo}: recorded PR URL {recorded!r} does not match verified URL {url!r}."
                )
        missing_urls = {
            repo: url
            for repo, url in recovered_urls.items()
            if repo not in current.pr_urls
        }
        if missing_urls:
            current.pr_urls.update(missing_urls)
            changed = True
        if current.finished_at is None:
            current.finished_at = completed_at
            changed = True

    state_manager.mutate(apply)
    _invalidate(cache)
    if changed:
        details = "; ".join(
            f"{entry.repo}: {entry.url} (merge {entry.merge_commit}, merged {entry.merged_at.isoformat()})"
            for entry in verified
        )
        log.append(
            task_slug,
            f"adopted merged PR metadata: {details}",
            action="adopt_merged",
        )
    return changed, verified


def _verify_repo(
    task: Task,
    repo_name: str,
    config: Any,
    shell: ShellRunner,
    pr_manager: PRManager,
) -> VerifiedMergedPR:
    repo_config = getattr(config, "repos", {}).get(repo_name)
    if repo_config is None:
        raise AdoptionError(f"{repo_name}: repository is not configured.")
    worktree = task.worktrees.get(repo_name)
    if worktree is None:
        raise AdoptionError(f"{repo_name}: task worktree is missing.")
    worktree = Path(worktree)
    if not worktree.is_dir():
        raise AdoptionError(f"{repo_name}: task worktree does not exist: {worktree}")

    base = (
        resolve_base(
            repo_name,
            repo_config,
            cli_base=None,
            base_map={},
            known_repos=getattr(config, "repos", {}).keys(),
            task_base=task.base_override,
        )
        or task.base_branch
    )
    if not base:
        raise AdoptionError(f"{repo_name}: no effective base branch is recorded.")

    _require_clean_worktree(repo_name, worktree, shell)
    head_sha = _git_value(repo_name, worktree, shell, "git rev-parse HEAD")
    if not _SHA_RE.fullmatch(head_sha):
        raise AdoptionError(f"{repo_name}: git returned an invalid HEAD SHA.")
    if not pr_manager.check_pushed_to_origin(worktree, task.branch):
        raise AdoptionError(
            f"{repo_name}: task branch {task.branch!r} is not pushed at its current commit."
        )

    github_repo = _github_repo(repo_name, worktree, shell)
    pr = _fetch_exact_pr(repo_name, worktree, shell, github_repo, task.branch, base)
    pr_head = pr["headRefOid"]
    if pr_head != head_sha:
        raise AdoptionError(
            f"{repo_name}: worktree HEAD does not match the merged PR head commit."
        )

    if not pr_manager.fetch_remote_branch(worktree, base):
        raise AdoptionError(f"{repo_name}: could not fetch origin/{base}.")
    reachability = shell.run(
        "git merge-base --is-ancestor "
        f"{shlex.quote(pr['mergeCommit']['oid'])} {shlex.quote(f'origin/{base}')}",
        cwd=worktree,
    )
    if reachability.returncode != 0:
        raise AdoptionError(
            f"{repo_name}: merge commit is not reachable from origin/{base}."
        )

    return VerifiedMergedPR(
        repo=repo_name,
        url=pr["url"],
        merge_commit=pr["mergeCommit"]["oid"],
        merged_at=_parse_merged_at(repo_name, pr["mergedAt"]),
    )


def _require_clean_worktree(repo_name: str, worktree: Path, shell: ShellRunner) -> None:
    result = shell.run("git status --porcelain --untracked-files=all", cwd=worktree)
    if result.returncode != 0:
        raise AdoptionError(f"{repo_name}: could not determine worktree status.")
    if result.stdout.strip():
        raise AdoptionError(f"{repo_name}: worktree has uncommitted changes.")


def _git_value(repo_name: str, worktree: Path, shell: ShellRunner, command: str) -> str:
    result = shell.run(command, cwd=worktree)
    if result.returncode != 0:
        raise AdoptionError(f"{repo_name}: {command} failed.")
    return result.stdout.strip()


def _github_repo(repo_name: str, worktree: Path, shell: ShellRunner) -> str:
    remote = _git_value(repo_name, worktree, shell, "git remote get-url origin")
    match = _GITHUB_REMOTE_RE.search(remote)
    if match is None:
        raise AdoptionError(f"{repo_name}: origin is not a GitHub repository.")
    return f"{match.group('owner')}/{match.group('repo')}"


def _fetch_exact_pr(
    repo_name: str,
    worktree: Path,
    shell: ShellRunner,
    github_repo: str,
    branch: str,
    base: str,
) -> dict[str, Any]:
    command = (
        "gh pr list "
        f"--repo {shlex.quote(github_repo)} --head {shlex.quote(branch)} --state all "
        "--json url,state,mergedAt,mergeCommit,headRefName,headRefOid,baseRefName --limit 100"
    )
    result = shell.run(command, cwd=worktree)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"gh exited {result.returncode}"
        raise AdoptionError(f"{repo_name}: GitHub PR query failed: {detail}")
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdoptionError(f"{repo_name}: GitHub returned invalid PR JSON.") from exc
    if (
        not isinstance(entries, list)
        or len(entries) != 1
        or not isinstance(entries[0], dict)
    ):
        raise AdoptionError(
            f"{repo_name}: expected exactly one PR for branch {branch!r}."
        )
    pr = entries[0]
    expected_url_prefix = f"https://github.com/{github_repo}/pull/"
    if not isinstance(pr.get("url"), str) or not pr["url"].startswith(
        expected_url_prefix
    ):
        raise AdoptionError(
            f"{repo_name}: GitHub returned a PR from the wrong repository."
        )
    if pr.get("headRefName") != branch:
        raise AdoptionError(
            f"{repo_name}: GitHub returned a PR with the wrong head branch."
        )
    if pr.get("baseRefName") != base:
        raise AdoptionError(
            f"{repo_name}: GitHub returned a PR with the wrong base branch."
        )
    if pr.get("state") != "MERGED":
        raise AdoptionError(f"{repo_name}: PR is not merged.")
    merge_commit = pr.get("mergeCommit")
    if not isinstance(merge_commit, dict) or not isinstance(
        merge_commit.get("oid"), str
    ):
        raise AdoptionError(f"{repo_name}: merged PR has no merge commit.")
    if not _SHA_RE.fullmatch(merge_commit["oid"]):
        raise AdoptionError(f"{repo_name}: merged PR returned an invalid merge commit.")
    if not isinstance(pr.get("headRefOid"), str) or not _SHA_RE.fullmatch(
        pr["headRefOid"]
    ):
        raise AdoptionError(f"{repo_name}: merged PR has no valid head commit.")
    _parse_merged_at(repo_name, pr.get("mergedAt"))
    return pr


def _parse_merged_at(repo_name: str, raw: object) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise AdoptionError(f"{repo_name}: merged PR has no merge timestamp.")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdoptionError(f"{repo_name}: merged PR timestamp is invalid.") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise AdoptionError(f"{repo_name}: merged PR timestamp lacks a timezone.")
    return value


def _invalidate(cache: ReconcileCache) -> None:
    payload = cache.read()
    if payload is not None:
        payload.fetched_at = 0.0
        cache.write(payload)
