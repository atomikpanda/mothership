"""Make sure a remote run executes the code you are actually looking at.

`--remote` works by having the run host materialize the task's branch **from
origin** (see `remote_exec.materialize_worktree`). Nothing on the caller side ever
checked that origin matched the local worktree, and nothing pushes during
development — `git push -u` happens at `mship finish`. So two things could happen
silently:

  * the branch was never pushed at all, and the remote failed with a materialize
    error that pointed at the remote rather than at the real cause; or
  * the branch was pushed at some earlier point, and the remote ran THAT revision.
    The output looked like a real result for code the operator was not editing.

The second is the dangerous one, and it is what this module exists to prevent. It
is deliberately conservative:

  * a clean repo whose branch is missing or behind on origin is **pushed** — that
    is unambiguously what the operator meant, and nothing is lost;
  * a repo with **tracked** modifications is **refused**, because inventing a
    commit out of someone's work in progress is not a decision this tool should
    make silently;
  * untracked files only **warn** — they cannot change what the push contains, but
    they also will not exist on the run host, which is worth saying out loud.

Every affected repo is checked, not just the one the operator happens to be
standing in: a task has a branch per repo and the run host materializes each one
separately, so a single-repo push leaves the others stale.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoState:
    """What one of a task's repos looks like relative to origin."""
    repo: str
    path: Path
    branch: str
    tracked_changes: bool
    untracked_only: bool
    needs_push: bool
    push_reason: str | None      # "no upstream" | "ahead of origin" | None


@dataclass(frozen=True)
class Preflight:
    """The verdict. `blocked` is non-empty when the run must not proceed."""
    states: list[RepoState]
    blocked: list[RepoState]
    to_push: list[RepoState]
    untracked: list[RepoState]

    @property
    def ok(self) -> bool:
        return not self.blocked


def _porcelain(shell, path: Path) -> tuple[bool, bool]:
    """(tracked_changes, untracked_only) for a worktree.

    `git status --porcelain` lists untracked entries as `??` and never lists
    gitignored files, so an ignored `.venv` does not make a repo look dirty.
    Untracked files are reported separately because they cannot alter what a push
    carries — refusing on them would block a run over a stray scratch file.
    """
    result = shell.run("git status --porcelain", cwd=path)
    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return False, False
    tracked = any(not ln.startswith("??") for ln in lines)
    return tracked, not tracked


def _push_need(shell, path: Path) -> tuple[bool, str | None]:
    """Whether this repo's HEAD is absent from or ahead of origin.

    Checked locally first so the common up-to-date case costs no network. A
    missing upstream is reported rather than treated as an error: on a task branch
    that has never been pushed it is the expected state.
    """
    upstream = shell.run(
        "git rev-parse --abbrev-ref --symbolic-full-name @{u}", cwd=path
    )
    if upstream.returncode != 0:
        return True, "no upstream"
    ahead = shell.run("git rev-list --count @{u}..HEAD", cwd=path)
    count = (ahead.stdout or "0").strip() or "0"
    try:
        if int(count) > 0:
            return True, "ahead of origin"
    except ValueError:
        # An unparseable count means we could not establish that origin is
        # current, so push rather than assume it is.
        return True, "could not compare with origin"
    return False, None


def inspect(task, shell) -> Preflight:
    """Read each of the task's worktrees. No network, no mutation."""
    states: list[RepoState] = []
    for repo, raw_path in sorted((task.worktrees or {}).items()):
        path = Path(raw_path)
        if not path.exists():
            continue                     # audit/prune surfaces missing worktrees
        tracked, untracked_only = _porcelain(shell, path)
        needs_push, reason = _push_need(shell, path)
        states.append(RepoState(
            repo=repo, path=path, branch=task.branch,
            tracked_changes=tracked, untracked_only=untracked_only,
            needs_push=needs_push, push_reason=reason,
        ))
    return Preflight(
        states=states,
        blocked=[s for s in states if s.tracked_changes],
        to_push=[s for s in states if s.needs_push and not s.tracked_changes],
        untracked=[s for s in states if s.untracked_only],
    )


def blocked_message(pre: Preflight) -> str:
    """Why the run was refused, and the exact commands that unblock it."""
    repos = ", ".join(s.repo for s in pre.blocked)
    lines = [
        f"uncommitted changes in {repos} — the run host materializes the task's "
        f"branch from origin, so it would run the last PUSHED revision, not what "
        f"you are editing.",
        "",
        "Commit and push first:",
        '  mship commit "<what changed>"        # commits across every task repo',
    ]
    for s in pre.blocked:
        lines.append(f'  git -C "{s.path}" push -u origin HEAD')
    return "\n".join(lines)


def push(pre: Preflight, shell) -> tuple[list[str], str | None]:
    """Push every clean repo that origin is missing or behind on.

    Returns `(pushed_repos, error)`. A push failure stops the run: proceeding
    after a failed push is exactly the silent-stale-code case this module exists
    to prevent.
    """
    pushed: list[str] = []
    for s in pre.to_push:
        result = shell.run(f"git push -u origin {s.branch}", cwd=s.path)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else f"exit {result.returncode}"
            return pushed, f"could not push {s.repo} ({s.push_reason}): {tail}"
        pushed.append(s.repo)
    return pushed, None
