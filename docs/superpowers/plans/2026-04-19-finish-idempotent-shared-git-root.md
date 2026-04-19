# Mship Finish — Idempotent + Shared-Git-Root + Upstream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `mship finish` groups affected repos that share `(git_root_or_self, branch, base)` into a single PR (one push + one `gh pr create`), records the URL on every group member, falls back to harvesting an existing PR on duplicate, and ensures `@{u}` tracking is set on the task branch after push.

**Architecture:** Add three `PRManager` helpers (`ensure_upstream`, `list_pr_for_branch`, duplicate-PR fallback inside `create_pr`). Build a pure `_build_pr_groups` function that turns `affected_repos + config + task + effective_bases` into `list[PRGroup]`. Rewire the `finish` repo loop to iterate groups — push once per group, ensure upstream, pre-check existing PR, create or harvest, record URL on all members. Update the coordination block template to show grouped members inline.

**Tech Stack:** Python 3.14, `shlex.quote`, stdlib `subprocess` via `ShellRunner`, pytest, local `file://` bare repos for integration.

**Reference spec:** `docs/superpowers/specs/2026-04-19-finish-idempotent-shared-git-root-design.md`

---

## File structure

**Modified files:**
- `src/mship/core/pr.py` — three new methods on `PRManager`: `ensure_upstream`, `list_pr_for_branch`, and `create_pr` gains a duplicate-PR fallback. Coordination block template updated (single-line change).
- `src/mship/cli/worktree.py` — `finish` command: add `_build_pr_groups` helper (inline, not a new module — small enough) and rewire the repo loop to iterate groups.
- `tests/core/test_pr.py` — new unit tests for each helper.
- `tests/cli/test_worktree.py` — new integration tests for shared-git_root grouping, idempotent retry, duplicate-PR fallback.

**New files:**
- None. `_build_pr_groups` lives in `worktree.py` alongside `finish` since it's ~40 lines and tightly coupled to the CLI's data model.

**Unchanged files:**
- `src/mship/core/reconcile/*` — audit filter untouched.
- `src/mship/core/repo_state.py::without_no_upstream_on_task_branch` — untouched (close-safety preserved).
- `src/mship/core/state.py` — state schema unchanged.

**Task ordering rationale:** Task 1 lands the three `PRManager` helpers — pure, no external coupling, easy to TDD. Task 2 lands the `_build_pr_groups` helper — pure function of config + task. Task 3 rewires `finish` to use both of the above, including coordination-block display. Task 4 is integration tests (file:// origin for real push, mocked gh) + finish PR.

---

## Task 1: `PRManager` helpers — `ensure_upstream`, `list_pr_for_branch`, duplicate-PR fallback

**Files:**
- Modify: `src/mship/core/pr.py`
- Modify: `tests/core/test_pr.py`

**Context:** Three small additions to `PRManager`, each individually testable against a mocked `ShellRunner`. These are called by `finish` in Task 3.

- [ ] **Step 1.1: Write failing tests for `ensure_upstream`**

Append to `tests/core/test_pr.py`:

```python
# --- ensure_upstream (issue #36 sibling, spec 2026-04-19) ---


def test_ensure_upstream_noop_when_already_set(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    # `git rev-parse --abbrev-ref --symbolic-full-name @{u}` returns 0 → already set.
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="origin/feat/x\n", stderr="")
    pr_mgr = PRManager(mock_shell)
    pr_mgr.ensure_upstream(Path("/repo"), "feat/x")
    assert mock_shell.run.call_count == 1
    called_cmd = mock_shell.run.call_args_list[0].args[0]
    assert "rev-parse" in called_cmd
    assert "@{u}" in called_cmd


def test_ensure_upstream_sets_tracking_when_missing(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    rc_results = [
        ShellResult(returncode=1, stdout="", stderr="fatal: no upstream"),  # rev-parse fails
        ShellResult(returncode=0, stdout="", stderr=""),                     # set-upstream-to succeeds
    ]
    mock_shell.run.side_effect = rc_results
    pr_mgr = PRManager(mock_shell)
    pr_mgr.ensure_upstream(Path("/repo"), "feat/x")
    assert mock_shell.run.call_count == 2
    second_cmd = mock_shell.run.call_args_list[1].args[0]
    assert "--set-upstream-to=origin/feat/x" in second_cmd
    assert "feat/x" in second_cmd
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/core/test_pr.py::test_ensure_upstream_noop_when_already_set tests/core/test_pr.py::test_ensure_upstream_sets_tracking_when_missing -v`
Expected: FAIL with `AttributeError: 'PRManager' object has no attribute 'ensure_upstream'`.

- [ ] **Step 1.3: Implement `ensure_upstream`**

Edit `src/mship/core/pr.py`. Add after `push_branch` (around line 33):

```python
    def ensure_upstream(self, repo_path: Path, branch: str) -> None:
        """Ensure `branch`'s tracking ref resolves. No-op when already set.

        `git push -u` normally sets tracking; this is belt-and-suspenders
        so `mship audit` doesn't report `no_upstream` after a finish where
        push succeeded but tracking config somehow wasn't written.
        """
        check = self._shell.run(
            "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
            cwd=repo_path,
        )
        if check.returncode == 0:
            return
        self._shell.run(
            f"git branch --set-upstream-to=origin/{shlex.quote(branch)} {shlex.quote(branch)}",
            cwd=repo_path,
        )
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `pytest tests/core/test_pr.py -v`
Expected: all tests pass (2 new + existing).

- [ ] **Step 1.5: Write failing tests for `list_pr_for_branch`**

Append to `tests/core/test_pr.py`:

```python
# --- list_pr_for_branch ---


def test_list_pr_for_branch_returns_url_when_present(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    mock_shell.run.return_value = ShellResult(
        returncode=0,
        stdout="https://github.com/org/repo/pull/17\n",
        stderr="",
    )
    pr_mgr = PRManager(mock_shell)
    url = pr_mgr.list_pr_for_branch(Path("/repo"), "feat/x")
    assert url == "https://github.com/org/repo/pull/17"
    cmd = mock_shell.run.call_args_list[0].args[0]
    assert "gh pr list" in cmd
    assert "--head" in cmd
    assert "--state all" in cmd
    assert "feat/x" in cmd


def test_list_pr_for_branch_returns_none_when_empty(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="\n", stderr="")
    pr_mgr = PRManager(mock_shell)
    assert pr_mgr.list_pr_for_branch(Path("/repo"), "feat/x") is None


def test_list_pr_for_branch_returns_none_on_gh_failure(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="error")
    pr_mgr = PRManager(mock_shell)
    assert pr_mgr.list_pr_for_branch(Path("/repo"), "feat/x") is None
```

- [ ] **Step 1.6: Run tests to verify they fail**

Run: `pytest tests/core/test_pr.py::test_list_pr_for_branch_returns_url_when_present -v`
Expected: FAIL with `AttributeError: 'PRManager' object has no attribute 'list_pr_for_branch'`.

- [ ] **Step 1.7: Implement `list_pr_for_branch`**

Add to `PRManager` in `src/mship/core/pr.py`, near `check_pr_state`:

```python
    def list_pr_for_branch(self, repo_path: Path, branch: str) -> str | None:
        """Return the URL of any PR (open/closed/merged) whose head is `branch`, or None.

        Used to:
        - Pre-check whether a PR already exists before calling `create_pr`
          (idempotent retry after mid-loop crash).
        - Fallback-harvest on `gh pr create`'s `already exists` error.
        """
        result = self._shell.run(
            f"gh pr list --head {shlex.quote(branch)} --state all "
            f"--json url -q '.[0].url'",
            cwd=repo_path,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        return url or None
```

- [ ] **Step 1.8: Run tests to verify they pass**

Run: `pytest tests/core/test_pr.py -v`
Expected: 5 new + existing, all pass.

- [ ] **Step 1.9: Write failing tests for `create_pr` duplicate-PR fallback**

Append to `tests/core/test_pr.py`:

```python
# --- create_pr duplicate-PR fallback ---


def test_create_pr_duplicate_harvests_existing_url(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    # First call: `gh pr create` fails with duplicate stderr.
    # Second call: `gh pr list ...` returns the existing URL.
    mock_shell.run.side_effect = [
        ShellResult(
            returncode=1,
            stdout="",
            stderr="a pull request for branch \"feat/x\" into branch \"main\" already exists",
        ),
        ShellResult(
            returncode=0,
            stdout="https://github.com/org/repo/pull/17\n",
            stderr="",
        ),
    ]
    pr_mgr = PRManager(mock_shell)
    url = pr_mgr.create_pr(
        repo_path=Path("/repo"), branch="feat/x",
        title="t", body="b", base="main",
    )
    assert url == "https://github.com/org/repo/pull/17"
    # gh pr create was called, then gh pr list was called
    assert "gh pr create" in mock_shell.run.call_args_list[0].args[0]
    assert "gh pr list" in mock_shell.run.call_args_list[1].args[0]


def test_create_pr_duplicate_but_list_fails_raises(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    # Duplicate stderr, then list also fails → falls through to RuntimeError.
    mock_shell.run.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="a pull request already exists"),
        ShellResult(returncode=1, stdout="", stderr="gh auth error"),
    ]
    pr_mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="Failed to create PR"):
        pr_mgr.create_pr(
            repo_path=Path("/repo"), branch="feat/x",
            title="t", body="b", base="main",
        )


def test_create_pr_non_duplicate_error_still_raises(mock_shell: MagicMock):
    """Regression: non-duplicate rc=1 errors still raise (existing behavior)."""
    from mship.core.pr import PRManager
    mock_shell.run.return_value = ShellResult(
        returncode=1, stdout="", stderr="fatal: some other error",
    )
    pr_mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="some other error"):
        pr_mgr.create_pr(
            repo_path=Path("/repo"), branch="feat/x",
            title="t", body="b", base="main",
        )
```

- [ ] **Step 1.10: Run tests to verify they fail**

Run: `pytest tests/core/test_pr.py::test_create_pr_duplicate_harvests_existing_url -v`
Expected: FAIL with `RuntimeError: Failed to create PR` (the duplicate fallback doesn't exist yet).

- [ ] **Step 1.11: Add the duplicate-PR fallback to `create_pr`**

Find `create_pr` in `src/mship/core/pr.py`:

```python
    def create_pr(
        self, repo_path: Path, branch: str, title: str, body: str,
        base: str | None = None,
    ) -> str:
        safe_title = shlex.quote(title)
        safe_body = shlex.quote(body)
        cmd = (
            f"gh pr create --title {safe_title} --body {safe_body} "
            f"--head {shlex.quote(branch)}"
        )
        if base is not None:
            cmd += f" --base {shlex.quote(base)}"
        result = self._shell.run(cmd, cwd=repo_path)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create PR: {result.stderr.strip()}"
            )
        return result.stdout.strip()
```

Replace the `if result.returncode != 0:` block:

```python
        if result.returncode != 0:
            stderr_lower = result.stderr.lower()
            if "already exists" in stderr_lower and "pull request" in stderr_lower:
                existing = self.list_pr_for_branch(repo_path, branch)
                if existing is not None:
                    return existing
            raise RuntimeError(
                f"Failed to create PR: {result.stderr.strip()}"
            )
        return result.stdout.strip()
```

- [ ] **Step 1.12: Run tests to verify they pass**

Run: `pytest tests/core/test_pr.py -v`
Expected: all pass (8 new + existing).

- [ ] **Step 1.13: Commit**

```bash
git add src/mship/core/pr.py tests/core/test_pr.py
git commit -m "feat(pr): ensure_upstream, list_pr_for_branch, duplicate-PR fallback"
mship journal "PRManager gains three helpers for idempotent finish: ensure_upstream, list_pr_for_branch, create_pr duplicate fallback" --action committed
```

---

## Task 2: `_build_pr_groups` — group affected repos by (git_root_or_self, branch, base)

**Files:**
- Modify: `src/mship/cli/worktree.py` (add helper near the `finish` command)
- Create: `tests/cli/test_finish_groups.py` (new, dedicated to grouping logic)

**Context:** Pure function. Takes the `affected_repos` list, `config`, `task`, and pre-computed `effective_bases` dict. Returns `list[PRGroup]`. No I/O.

- [ ] **Step 2.1: Write failing tests**

Create `tests/cli/test_finish_groups.py`:

```python
"""Tests for `_build_pr_groups` — pure grouping logic for mship finish.

Shared-git_root repos that push to the same branch resolve to one gh PR.
This helper groups them so finish can make one push + one create call
instead of one per repo.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.state import Task


def _cfg(**repos: RepoConfig) -> WorkspaceConfig:
    return WorkspaceConfig(workspace="t", repos=dict(repos))


def _task(affected: list[str], branch: str = "feat/x") -> Task:
    return Task(
        slug="t", description="t", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=affected, worktrees={},
        branch=branch, base_branch="main",
    )


def test_three_repos_shared_git_root_form_one_group(tmp_path: Path):
    from mship.cli.worktree import _build_pr_groups
    config = _cfg(
        tailrd=RepoConfig(path=tmp_path / "tailrd", type="service"),
        infra=RepoConfig(path=Path("."), git_root="tailrd", type="service"),
        web=RepoConfig(path=Path("web"), git_root="tailrd", type="service"),
    )
    task = _task(["infra", "tailrd", "web"])
    effective_bases = {"infra": "main", "tailrd": "main", "web": "main"}

    groups = _build_pr_groups(
        ["infra", "tailrd", "web"], config, task, effective_bases,
    )
    assert len(groups) == 1
    g = groups[0]
    assert sorted(g.members) == ["infra", "tailrd", "web"]
    assert g.rep_name == "tailrd"  # git_root parent preferred
    assert g.base == "main"


def test_two_shared_plus_one_standalone_form_two_groups(tmp_path: Path):
    from mship.cli.worktree import _build_pr_groups
    config = _cfg(
        tailrd=RepoConfig(path=tmp_path / "tailrd", type="service"),
        infra=RepoConfig(path=Path("."), git_root="tailrd", type="service"),
        api=RepoConfig(path=tmp_path / "api", type="service"),
    )
    task = _task(["infra", "tailrd", "api"])
    effective_bases = {"infra": "main", "tailrd": "main", "api": "main"}

    groups = _build_pr_groups(
        ["infra", "tailrd", "api"], config, task, effective_bases,
    )
    assert len(groups) == 2
    by_rep = {g.rep_name: g for g in groups}
    assert sorted(by_rep["tailrd"].members) == ["infra", "tailrd"]
    assert by_rep["api"].members == ["api"]


def test_all_standalone_form_n_groups(tmp_path: Path):
    from mship.cli.worktree import _build_pr_groups
    config = _cfg(
        api=RepoConfig(path=tmp_path / "api", type="service"),
        web=RepoConfig(path=tmp_path / "web", type="service"),
    )
    task = _task(["api", "web"])
    effective_bases = {"api": "main", "web": "main"}

    groups = _build_pr_groups(["api", "web"], config, task, effective_bases)
    assert len(groups) == 2
    assert sorted(g.rep_name for g in groups) == ["api", "web"]


def test_git_root_parent_not_in_affected_repos_falls_back_to_first_member(tmp_path: Path):
    """Pathological: user passes --repos that excludes the git_root parent."""
    from mship.cli.worktree import _build_pr_groups
    config = _cfg(
        tailrd=RepoConfig(path=tmp_path / "tailrd", type="service"),
        infra=RepoConfig(path=Path("."), git_root="tailrd", type="service"),
        web=RepoConfig(path=Path("web"), git_root="tailrd", type="service"),
    )
    task = _task(["infra", "web"])  # tailrd not included
    effective_bases = {"infra": "main", "web": "main"}

    groups = _build_pr_groups(["infra", "web"], config, task, effective_bases)
    assert len(groups) == 1
    g = groups[0]
    # Parent not in affected; representative falls back to first member in input order.
    assert g.rep_name == "infra"


def test_heterogeneous_bases_within_group_raises(tmp_path: Path):
    """Defensive: if shared-git_root members somehow have different bases,
    we surface an error rather than pick one silently."""
    from mship.cli.worktree import _build_pr_groups
    config = _cfg(
        tailrd=RepoConfig(path=tmp_path / "tailrd", type="service"),
        infra=RepoConfig(path=Path("."), git_root="tailrd", type="service"),
    )
    task = _task(["infra", "tailrd"])
    effective_bases = {"infra": "main", "tailrd": "develop"}  # mismatch

    with pytest.raises(ValueError, match="mixed effective_bases"):
        _build_pr_groups(["infra", "tailrd"], config, task, effective_bases)


def test_group_rep_path_uses_git_root_effective_path(tmp_path: Path):
    """Group's rep_path should be the parent's effective path, not a subdir."""
    from mship.cli.worktree import _build_pr_groups
    parent_path = tmp_path / "tailrd"
    parent_path.mkdir()
    config = _cfg(
        tailrd=RepoConfig(path=parent_path, type="service"),
        web=RepoConfig(path=Path("web"), git_root="tailrd", type="service"),
    )
    task = _task(["tailrd", "web"])
    effective_bases = {"tailrd": "main", "web": "main"}

    groups = _build_pr_groups(["tailrd", "web"], config, task, effective_bases)
    assert len(groups) == 1
    # Representative is tailrd, rep_path = tailrd's effective path (not web subdir).
    assert groups[0].rep_path == parent_path.resolve()
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `pytest tests/cli/test_finish_groups.py -v`
Expected: FAIL with `ImportError: cannot import name '_build_pr_groups' from 'mship.cli.worktree'`.

- [ ] **Step 2.3: Add `PRGroup` dataclass and `_build_pr_groups` function**

Edit `src/mship/cli/worktree.py`. Near the top of the file (after imports, before the `app = typer.Typer()` line or wherever top-level helpers live), add:

```python
from dataclasses import dataclass as _dataclass_pr


@_dataclass_pr
class PRGroup:
    """A set of affected repos that share a single GitHub PR.

    Members all push to the same branch on the same git repo (same
    git_root_or_self) with the same effective base.
    """
    rep_name: str           # repo name driving push + create_pr calls
    rep_path: Path          # absolute path to run git/gh from
    members: list[str]      # all repos sharing this PR (includes rep_name)
    branch: str
    base: str | None


def _build_pr_groups(
    affected_repos: list[str],
    config: "WorkspaceConfig",
    task: "Task",
    effective_bases: dict[str, str | None],
) -> list[PRGroup]:
    """Group repos that share (git_root_or_self, branch, base) into PR groups.

    For repos with `git_root` set, the group key uses the git_root name.
    For repos without `git_root`, the key uses the repo's own name. One
    group per distinct key.

    Representative selection (determines where push + gh calls run):
    - If the group's git_root_or_self IS in `affected_repos`, use it.
    - Else, fall back to the first member in `affected_repos` input order.

    Raises ValueError if a group's members have heterogeneous effective
    bases (defensive — not expected for shared git_root).
    """
    from collections import defaultdict

    def _root_of(repo_name: str) -> str:
        r = config.repos[repo_name]
        return r.git_root if r.git_root is not None else repo_name

    def _effective_path(repo_name: str) -> Path:
        r = config.repos[repo_name]
        if r.git_root is not None:
            parent = config.repos[r.git_root]
            return (Path(parent.path) / Path(r.path)).resolve()
        return Path(r.path).resolve()

    # Preserve input order within each group by iterating affected_repos
    # and appending.
    buckets: dict[str, list[str]] = defaultdict(list)
    for repo_name in affected_repos:
        buckets[_root_of(repo_name)].append(repo_name)

    groups: list[PRGroup] = []
    for root, members in buckets.items():
        # Sanity check: bases should match within a group.
        bases = {effective_bases.get(m) for m in members}
        if len(bases) > 1:
            raise ValueError(
                f"group {root!r} has mixed effective_bases: {sorted(str(b) for b in bases)}"
            )
        base = next(iter(bases))

        # Representative: prefer the git_root parent if in affected_repos,
        # else the first member in input order.
        if root in members:
            rep_name = root
        else:
            rep_name = members[0]
        rep_path = _effective_path(rep_name)

        groups.append(PRGroup(
            rep_name=rep_name,
            rep_path=rep_path,
            members=list(members),
            branch=task.branch,
            base=base,
        ))
    return groups
```

Note: the forward references `"WorkspaceConfig"` and `"Task"` in the signature are used because those classes may not yet be imported at the point we add this helper. If the file already imports them at the top, use the unquoted names.

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `pytest tests/cli/test_finish_groups.py -v`
Expected: 6 passed.

- [ ] **Step 2.5: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_finish_groups.py
git commit -m "feat(finish): _build_pr_groups — group shared-git_root repos"
mship journal "pure grouping helper; 6 unit tests covering shared git_root, standalone repos, parent-missing fallback, mixed-base defense" --action committed
```

---

## Task 3: Rewire `finish` loop to iterate groups

**Files:**
- Modify: `src/mship/cli/worktree.py` (the `finish` command's repo loop + coordination-block template)

**Context:** The existing loop in `finish` iterates `ordered` (topo-sorted `affected_repos`). We replace it with a loop over `_build_pr_groups(...)`. Each group does one push + ensure_upstream + list_pr_for_branch + create_pr-or-harvest + record URL on all members. The coordination block's `pr_list` entries gain a `members` field; the block renders `tailrd (+infra, web)` when a group has >1 member.

- [ ] **Step 3.1: Rewire the main repo loop**

Edit `src/mship/cli/worktree.py` inside the `finish` command. Find the repo loop starting at approximately line 709 (`for i, repo_name in enumerate(ordered, 1):`).

The existing loop structure is:
1. Skip-if-already-has-PR (non-force) path
2. `--force` re-push path
3. Fresh push + create_pr path

Replace with a group-based loop. Locate the existing `for i, repo_name in enumerate(ordered, 1):` block (~line 709) and its body (~lines 709–820). Replace that entire block with:

```python
        groups = _build_pr_groups(ordered, config, task, effective_bases)

        for i, group in enumerate(groups, 1):
            members_str = (
                group.rep_name
                if len(group.members) == 1
                else f"{group.rep_name} (+{', '.join(m for m in group.members if m != group.rep_name)})"
            )

            # --- Skip path: every member already has the PR URL recorded.
            all_members_have_url = all(m in task.pr_urls for m in group.members)
            if all_members_have_url and not force:
                url = task.pr_urls[group.rep_name]
                if output.is_tty:
                    output.print(f"  {members_str}: already has PR {url}")
                pr_list.append({
                    "repo": group.rep_name,
                    "members": list(group.members),
                    "url": url,
                    "order": i,
                    "base": group.base,
                })
                continue

            # --- --force re-push path: branch exists on origin, push new commits.
            if all_members_have_url and force:
                try:
                    pr_mgr.push_branch(group.rep_path, task.branch)
                except RuntimeError as e:
                    output.error(f"{group.rep_name}: {e}")
                    raise typer.Exit(code=1)
                pr_mgr.ensure_upstream(group.rep_path, task.branch)
                repushed_repos.extend(group.members)
                url = task.pr_urls[group.rep_name]
                if output.is_tty:
                    output.print(f"  {members_str}: {task.branch} re-pushed to {url}")
                pr_list.append({
                    "repo": group.rep_name,
                    "members": list(group.members),
                    "url": url,
                    "order": i,
                    "base": group.base,
                })
                continue

            # --- Fresh path: push, ensure_upstream, find-or-create PR.
            try:
                pr_mgr.push_branch(group.rep_path, task.branch)
            except RuntimeError as e:
                output.error(f"{group.rep_name}: {e}")
                raise typer.Exit(code=1)

            pr_mgr.ensure_upstream(group.rep_path, task.branch)

            # Idempotent check: did a PR already get created for this branch?
            existing_url = pr_mgr.list_pr_for_branch(group.rep_path, task.branch)

            if existing_url is not None:
                pr_url = existing_url
                if output.is_tty:
                    output.print(f"  {members_str}: found existing PR {pr_url}")
            else:
                # Build the PR body — appends `Closes #N` for any GitHub issue
                # references in the task description, log entries, or commit subjects.
                from mship.core.issue_refs import append_closes_footer, extract_issue_refs
                texts: list[str] = [task.description]
                try:
                    entries = container.log_manager().read(task.slug)
                    for e in entries:
                        if e.message:
                            texts.append(e.message)
                        if e.action:
                            texts.append(e.action)
                        if e.open_question:
                            texts.append(e.open_question)
                except Exception:
                    pass
                try:
                    eff_base = group.base or "HEAD"
                    import shlex as _shlex
                    subjects_res = shell.run(
                        f"git log --format=%s origin/{_shlex.quote(eff_base)}..{_shlex.quote(task.branch)}",
                        cwd=group.rep_path,
                    )
                    if subjects_res.returncode == 0:
                        for line in subjects_res.stdout.splitlines():
                            if line.strip():
                                texts.append(line)
                except Exception:
                    pass
                pr_body_base = custom_body if custom_body is not None else task.description
                pr_body = append_closes_footer(pr_body_base, extract_issue_refs(texts))

                try:
                    pr_url = pr_mgr.create_pr(
                        repo_path=group.rep_path,
                        branch=task.branch,
                        title=task.description,
                        body=pr_body,
                        base=group.base,
                    )
                except RuntimeError as e:
                    output.error(f"{group.rep_name}: {e}")
                    raise typer.Exit(code=1)

            # Store URL on every group member (crash-safe: single state mutation).
            def _record_group(s, members=list(group.members), u=pr_url):
                for name in members:
                    s.tasks[t.slug].pr_urls[name] = u
            state_mgr.mutate(_record_group)
            for name in group.members:
                task.pr_urls[name] = pr_url

            pr_list.append({
                "repo": group.rep_name,
                "members": list(group.members),
                "url": pr_url,
                "order": i,
                "base": group.base,
            })

            base_label = group.base or "(default)"
            if output.is_tty:
                output.print(f"  {members_str}: {task.branch} → {base_label}  ✓ {pr_url}")
```

Key differences vs. the old loop:
- Iterates groups instead of individual repos. One push per group.
- `ensure_upstream` called after every `push_branch`.
- Pre-checks `list_pr_for_branch` before calling `create_pr`.
- Records URL on ALL members of the group in one state mutation (atomic per-group instead of per-repo).
- Adds `"members"` key to each `pr_list` entry.
- `members_str` renders "tailrd (+infra, web)" when a group has more than one member.

Note: the `--force` existing re-push path used to not call `ensure_upstream`. Adding it here is safe (idempotent) and closes an edge case where a user's previous finish somehow didn't set upstream.

- [ ] **Step 3.2: Update the coordination-block rendering path**

Find the code that builds the coordination block and updates PR bodies (around line 824 in the existing file, `if len(pr_list) > 1 and not force:`).

The `build_coordination_block` method in `src/mship/core/pr.py` takes `pr_list` and renders a Markdown table. Today each entry shows one repo. After this change, entries have `members`. Update the method to show grouped repos.

Find in `src/mship/core/pr.py`, the `build_coordination_block` method (around line 177):

```python
    def build_coordination_block(
        self,
        task_slug: str,
        prs: list[dict],
        current_repo: str,
    ) -> str:
        if len(prs) <= 1:
            return ""

        lines = [
            "",
            "---",
            "",
            "## Cross-repo coordination (mothership)",
            "",
            f"This PR is part of a coordinated change: `{task_slug}`",
            "",
            "| # | Repo | PR | Merge order |",
            "|---|------|----|-------------|",
        ]

        for pr in prs:
            if pr["repo"] == current_repo:
                order_label = "this PR"
            elif pr["order"] == 1:
                order_label = "merge first"
            else:
                order_label = f"merge #{pr['order']}"
            lines.append(
                f"| {pr['order']} | {pr['repo']} | {pr['url']} | {order_label} |"
            )

        deps_note = " → ".join(pr["repo"] for pr in prs)
        lines.append("")
        lines.append(f"⚠ Merge in order: {deps_note}")

        return "\n".join(lines)
```

Replace the rendering loop to show grouped members:

```python
    def build_coordination_block(
        self,
        task_slug: str,
        prs: list[dict],
        current_repo: str,
    ) -> str:
        if len(prs) <= 1:
            return ""

        lines = [
            "",
            "---",
            "",
            "## Cross-repo coordination (mothership)",
            "",
            f"This PR is part of a coordinated change: `{task_slug}`",
            "",
            "| # | Repo | PR | Merge order |",
            "|---|------|----|-------------|",
        ]

        for pr in prs:
            members = pr.get("members", [pr["repo"]])
            repo_label = (
                pr["repo"] if len(members) == 1
                else f"{pr['repo']} (+{', '.join(m for m in members if m != pr['repo'])})"
            )
            if current_repo in members:
                order_label = "this PR"
            elif pr["order"] == 1:
                order_label = "merge first"
            else:
                order_label = f"merge #{pr['order']}"
            lines.append(
                f"| {pr['order']} | {repo_label} | {pr['url']} | {order_label} |"
            )

        deps_note = " → ".join(pr["repo"] for pr in prs)
        lines.append("")
        lines.append(f"⚠ Merge in order: {deps_note}")

        return "\n".join(lines)
```

The only substantive change: `repo_label` now shows `"tailrd (+infra, web)"` when the group has >1 member, and `current_repo` matching uses the members list rather than just the rep name (so if the per-PR body being built is tailrd's, the current-PR row is marked even though `pr['repo']` is the same).

Actually since the PR body is ONE PR per group (the same URL for all members), `current_repo` comparison just needs to match any member. `current_repo in members` captures that.

- [ ] **Step 3.3: Run all related tests**

Run: `pytest tests/core/test_pr.py tests/cli/test_worktree.py tests/cli/test_finish_groups.py -v`

Expected: all tests pass. The existing `test_finish_creates_prs` etc. should still pass because single-repo tasks produce single-member groups that render identically to before.

If an existing test fails, likely because it asserts on the exact text of the coordination block or the `pr_list` shape. Update the assertion to tolerate the new `members` key.

- [ ] **Step 3.4: Commit**

```bash
git add src/mship/cli/worktree.py src/mship/core/pr.py
git commit -m "feat(finish): iterate PR groups; idempotent retry; shared-git_root records on all members"
mship journal "finish loop rewired to iterate _build_pr_groups; ensure_upstream + list_pr_for_branch + create_pr harvest form the idempotent path" --action committed
```

---

## Task 4: Integration tests + manual pre-ship verification

**Files:**
- Modify: `tests/cli/test_worktree.py` — new integration tests.

**Context:** With the logic in place, add end-to-end tests that exercise `mship finish` against a mocked-gh + real-git environment. These catch regressions in the wiring between `_build_pr_groups`, the three PR helpers, and the CLI handler. Manual smoke against a real gh remote is explicitly out of scope (per the brainstorming decision).

- [ ] **Step 4.1: Write failing integration test for shared-git_root grouping**

Append to `tests/cli/test_worktree.py`:

```python
def test_finish_shared_git_root_creates_one_pr_records_on_all(configured_git_app: Path):
    """Two repos sharing git_root: one gh pr create call, both get the URL."""
    from mship.cli import container as cli_container

    # Extend the workspace with a shared-git_root pair.
    cfg_path = configured_git_app / "mothership.yaml"
    cfg_path.write_text(cfg_path.read_text() + """
  infra:
    path: .
    git_root: shared
    type: service
""")

    runner.invoke(app, ["spawn", "group prs", "--repos", "shared,infra", "--skip-setup"])

    create_pr_call_count = 0

    def mock_run(cmd, cwd, env=None):
        nonlocal create_pr_call_count
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            # Pretend upstream is already set after push.
            return ShellResult(returncode=0, stdout="origin/feat/group-prs", stderr="")
        if "gh pr list --head" in cmd:
            # No existing PR on first call.
            return ShellResult(returncode=0, stdout="\n", stderr="")
        if "gh pr create" in cmd:
            create_pr_call_count += 1
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/42\n", stderr="")
        if "gh pr view" in cmd:
            return ShellResult(returncode=0, stdout="body text", stderr="")
        if "gh pr edit" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git log --format=%s" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "group-prs"])
    assert result.exit_code == 0, result.output
    assert create_pr_call_count == 1, f"Expected 1 gh pr create call, got {create_pr_call_count}"

    from mship.core.state import StateManager
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    pr_urls = state.tasks["group-prs"].pr_urls
    assert pr_urls.get("shared") == "https://github.com/org/shared/pull/42"
    assert pr_urls.get("infra") == "https://github.com/org/shared/pull/42"

    cli_container.shell.reset_override()
```

- [ ] **Step 4.2: Write failing test for idempotent retry (external/pre-existing PR)**

Append:

```python
def test_finish_harvests_existing_pr_instead_of_creating(configured_git_app: Path):
    """If a PR for the branch already exists (manual or prior mship run),
    finish harvests it via `gh pr list --head` without calling `gh pr create`."""
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "reuse pr", "--repos", "shared", "--skip-setup"])

    create_pr_called = False

    def mock_run(cmd, cwd, env=None):
        nonlocal create_pr_called
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/feat/reuse-pr", stderr="")
        if "gh pr list --head" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/88\n", stderr="")
        if "gh pr create" in cmd:
            create_pr_called = True
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "reuse-pr"])
    assert result.exit_code == 0, result.output
    assert create_pr_called is False, "gh pr create should not be called when PR already exists"

    from mship.core.state import StateManager
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert state.tasks["reuse-pr"].pr_urls.get("shared") == "https://github.com/org/shared/pull/88"

    cli_container.shell.reset_override()
```

- [ ] **Step 4.3: Write failing test for duplicate-PR stderr fallback**

Append:

```python
def test_finish_harvests_on_create_pr_duplicate_stderr(configured_git_app: Path):
    """When `gh pr list` returns empty but `gh pr create` then errors with
    'already exists' (race), finish harvests via a second list call."""
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "race pr", "--repos", "shared", "--skip-setup"])

    list_call_count = 0

    def mock_run(cmd, cwd, env=None):
        nonlocal list_call_count
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/feat/race-pr", stderr="")
        if "gh pr list --head" in cmd:
            list_call_count += 1
            if list_call_count == 1:
                # First call (pre-create check): no PR yet.
                return ShellResult(returncode=0, stdout="\n", stderr="")
            else:
                # Second call (fallback after create failed): PR exists now.
                return ShellResult(
                    returncode=0,
                    stdout="https://github.com/org/shared/pull/99\n",
                    stderr="",
                )
        if "gh pr create" in cmd:
            return ShellResult(
                returncode=1, stdout="",
                stderr="a pull request for branch \"feat/race-pr\" into branch \"main\" already exists",
            )
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "race-pr"])
    assert result.exit_code == 0, result.output
    assert list_call_count == 2, "Expected pre-check + fallback list calls"

    from mship.core.state import StateManager
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert state.tasks["race-pr"].pr_urls.get("shared") == "https://github.com/org/shared/pull/99"

    cli_container.shell.reset_override()
```

- [ ] **Step 4.4: Write failing test for ensure_upstream behavior**

Append:

```python
def test_finish_calls_ensure_upstream_after_push(configured_git_app: Path):
    """ensure_upstream fires after push; if @{u} fails, set-upstream-to runs."""
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "upstream check", "--repos", "shared", "--skip-setup"])

    set_upstream_called = False

    def mock_run(cmd, cwd, env=None):
        nonlocal set_upstream_called
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            # Simulate: push succeeded but tracking config wasn't set.
            return ShellResult(returncode=1, stdout="", stderr="fatal: no upstream")
        if "--set-upstream-to=origin/" in cmd:
            set_upstream_called = True
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr list --head" in cmd:
            return ShellResult(returncode=0, stdout="\n", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "upstream-check"])
    assert result.exit_code == 0, result.output
    assert set_upstream_called, "ensure_upstream should have run set-upstream-to"

    cli_container.shell.reset_override()
```

- [ ] **Step 4.5: Run all integration tests**

Run: `pytest tests/cli/test_worktree.py -v`
Expected: all pass — the 4 new tests + every existing test.

If existing `test_finish_creates_prs` or siblings fail, they're likely mocking `gh pr list` differently or asserting on `pr_list` shape. Update assertions to tolerate the new `members` key and the new pre-create `gh pr list` call.

- [ ] **Step 4.6: Run the full suite**

Run: `pytest tests/ 2>&1 | tail -5`
Expected: all pass. (Baseline ~885 tests; this plan adds ~17 new tests — expect ~900+.)

- [ ] **Step 4.7: Reinstall tool**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/finish-idempotent-on-existing-pr-shared-gitroot-records-on-all-audit-sees-pushed-upstream
uv tool install --reinstall --from . mothership
```

- [ ] **Step 4.8: Open PR**

Write to `/tmp/finish-bundle-body.md`:

```markdown
## Summary

Bundled fix for three sharp edges on `mship finish` reported from a real session on 2026-04-18:

1. **Finish wasn't idempotent** when a PR already existed for the branch. Caught on shared-git_root repos (`infra` + `tailrd` sharing `path: .`): first run created PR #17, second invocation for `tailrd` errored `a pull request for branch ... already exists`. `finished_at` never stamped.
2. **Shared `git_root` repos were only recorded on one entry** in `pr_urls`, leaving the others looking unfinished to `mship reconcile` / `mship status`.
3. **`mship audit` reported `no_upstream`** on task branches after finish, even though `git push -u` should have set tracking config.

## Fix shape

- **Grouping:** `finish` now groups affected repos by `(git_root_or_self, branch, base)`. One push + one `gh pr create` per group; the resulting URL is recorded on every member.
- **Idempotency:** Before `create_pr`, `finish` calls `gh pr list --head <branch> --state all` to harvest an existing PR if present. Belt-and-suspenders: `create_pr` also catches the `already exists` stderr and harvests via the same list call.
- **Upstream:** New `PRManager.ensure_upstream` runs after every push. No-op if `@{u}` resolves; otherwise explicitly sets tracking with `git branch --set-upstream-to=origin/<branch> <branch>`. The audit filter's `finished_at is None` condition is UNTOUCHED — close-safety preserved.

## Scope boundaries

- No change to `mship audit`'s close-safety filter (still protects against deleting unpushed work).
- No auto-close on retry. Idempotency ≠ "auto-advance to close."
- No new CLI flags. `--force` keeps its existing semantics.
- No retroactive unblock for tasks currently stuck with `finished_at=None` — they need manual recovery; this fix prevents the bug going forward.
- Real-gh manual smoke explicitly scoped out (per brainstorming decision). Coverage is comprehensive via unit + integration tests with mocked gh.

## Changes

- `src/mship/core/pr.py`:
  - New `PRManager.ensure_upstream(repo_path, branch)`.
  - New `PRManager.list_pr_for_branch(repo_path, branch) -> str | None`.
  - `create_pr` gains a duplicate-PR fallback — on `"already exists"` stderr, harvests via `list_pr_for_branch`.
  - `build_coordination_block` shows `"tailrd (+infra, web)"` when a group has multiple members.
- `src/mship/cli/worktree.py`:
  - New `PRGroup` dataclass + `_build_pr_groups` pure helper.
  - `finish` repo loop rewired to iterate groups. Each group: push once, `ensure_upstream`, pre-check `list_pr_for_branch`, create or harvest, record URL on all members.

## Test plan

- [x] `tests/core/test_pr.py`: 8 new unit tests (ensure_upstream × 2, list_pr_for_branch × 3, create_pr duplicate fallback × 3).
- [x] `tests/cli/test_finish_groups.py`: 6 new tests covering grouping logic (shared git_root, standalone, parent-missing fallback, heterogeneous-base defense, rep_path resolution).
- [x] `tests/cli/test_worktree.py`: 4 new integration tests (shared-git_root one-PR record-on-all, harvest-existing-PR, duplicate-stderr race, ensure_upstream post-push).
- [x] Full suite: all pass.

Closes the three sharp edges Bailey surfaced on 2026-04-18.
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/finish-idempotent-on-existing-pr-shared-gitroot-records-on-all-audit-sees-pushed-upstream
mship finish --body-file /tmp/finish-bundle-body.md
```

Expected: PR URL returned. (Ironically, this PR benefits from its own fix — even though the worktree has a single repo, the retry + ensure_upstream paths are exercised.)

---

## Done when

- [x] `PRManager.ensure_upstream`, `PRManager.list_pr_for_branch`, and `create_pr`'s duplicate-PR fallback exist with unit-test coverage.
- [x] `_build_pr_groups` + `PRGroup` exist, pure function, 6 unit tests green.
- [x] `finish` loop iterates groups; `ensure_upstream` called after every push; existing-PR check runs before create; URL recorded on every group member.
- [x] `build_coordination_block` renders grouped members.
- [x] `mship finish` retry is idempotent — a retry after a mid-loop failure harvests the existing PR.
- [x] Shared-git_root repos all have their `pr_urls` populated with the same URL.
- [x] Audit's close-safety filter (`without_no_upstream_on_task_branch` with `finished_at is None` condition) is unchanged.
- [x] Full pytest green. No manual gh smoke; coverage via mocks + file-level git.
