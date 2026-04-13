# Exclude mship Worktrees from `extra_worktrees` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the `extra_worktrees` audit check from firing on mship's own worktrees so `mship finish` and `mship audit` work in normal workflow.

**Architecture:** Add a `known_worktree_paths: frozenset[Path]` parameter to `audit_repos`, filtered inside `_probe_git_wide` before counting. A new `audit_gate.collect_known_worktree_paths(state_manager)` helper builds the set from every task in state. All four call sites (spawn, finish, `mship audit`, `mship sync`) use the helper.

**Tech Stack:** Python 3.12+, Pydantic (for state model), pathlib resolve-based comparison.

**Spec:** `docs/superpowers/specs/2026-04-13-audit-known-worktrees-design.md`

---

## File Structure

**Modify:**
- `src/mship/core/repo_state.py` — extract `_list_worktree_paths`; thread `known_worktree_paths` through `audit_repos` → `_probe_git_wide`; refine the `extra_worktrees` message.
- `src/mship/core/audit_gate.py` — add `collect_known_worktree_paths(state_manager) -> frozenset[Path]`.
- `src/mship/cli/audit.py` — pass `known_worktree_paths` to `audit_repos`.
- `src/mship/cli/sync.py` — pass `known_worktree_paths` to `audit_repos`.
- `src/mship/cli/worktree.py` — spawn and finish gates pass `known_worktree_paths` to `audit_repos`.
- `tests/core/test_repo_state.py` — add unit tests for `_list_worktree_paths` and the filter logic.
- `tests/core/test_audit_gate.py` — add unit test for `collect_known_worktree_paths`.
- `tests/cli/test_audit.py` — add integration test: known worktree clean; foreign worktree flagged.
- `tests/test_finish_integration.py` — assert `mship finish` no longer blocks on its own worktree.

---

## Task 1: Core refactor — `_list_worktree_paths` + `known_worktree_paths`

**Files:**
- Modify: `src/mship/core/repo_state.py`
- Test: `tests/core/test_repo_state.py`

- [ ] **Step 1: Write the failing unit tests**

Append to `tests/core/test_repo_state.py`:
```python
from pathlib import Path as _Path

from mship.core.repo_state import _list_worktree_paths


class _FakeShell:
    def __init__(self, stdout: str, rc: int = 0):
        self._stdout = stdout
        self._rc = rc

    def run(self, cmd: str, cwd, env=None):
        from mship.util.shell import ShellResult
        return ShellResult(returncode=self._rc, stdout=self._stdout, stderr="")


def test_list_worktree_paths_parses_porcelain():
    porcelain = (
        "worktree /abs/main\n"
        "HEAD abc\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /abs/feat-x\n"
        "HEAD def\n"
        "branch refs/heads/feat/x\n"
    )
    shell = _FakeShell(porcelain)
    paths = _list_worktree_paths(shell, _Path("/abs/main"))
    assert [str(p) for p in paths] == ["/abs/main", "/abs/feat-x"]


def test_list_worktree_paths_empty_output():
    shell = _FakeShell("")
    assert _list_worktree_paths(shell, _Path("/abs/main")) == []


def test_list_worktree_paths_returns_resolved_paths(tmp_path):
    # The real resolve call normalizes "." and ".." and symlinks. Simulate by
    # emitting an absolute but unresolved path.
    unresolved = str(tmp_path / "a" / ".." / "a")
    shell = _FakeShell(f"worktree {unresolved}\nHEAD abc\n")
    (tmp_path / "a").mkdir()
    paths = _list_worktree_paths(shell, tmp_path)
    assert paths == [(tmp_path / "a").resolve()]


def test_audit_known_worktree_suppresses_extra_worktrees(audit_workspace):
    """A worktree registered in known_worktree_paths is not counted as extra."""
    import subprocess
    import os
    from mship.core.config import ConfigLoader
    from mship.core.repo_state import audit_repos
    from mship.util.shell import ShellRunner

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    clone = audit_workspace / "cli"
    wt = audit_workspace / "cli-wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "scratch"],
        cwd=clone, check=True, capture_output=True, env=env,
    )

    cfg = ConfigLoader.load(audit_workspace / "mothership.yaml")
    shell = ShellRunner()

    # Not excluded → extra_worktrees fires
    rep_open = audit_repos(cfg, shell, names=["cli"])
    codes_open = {i.code for r in rep_open.repos if r.name == "cli" for i in r.issues}
    assert "extra_worktrees" in codes_open

    # Excluded → no extra_worktrees
    known = frozenset({wt.resolve()})
    rep_known = audit_repos(cfg, shell, names=["cli"], known_worktree_paths=known)
    codes_known = {i.code for r in rep_known.repos if r.name == "cli" for i in r.issues}
    assert "extra_worktrees" not in codes_known


def test_audit_foreign_worktree_still_fires(audit_workspace):
    """A worktree NOT in known_worktree_paths still counts as extra."""
    import subprocess
    import os
    from mship.core.config import ConfigLoader
    from mship.core.repo_state import audit_repos
    from mship.util.shell import ShellRunner

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    clone = audit_workspace / "cli"
    wt_known = audit_workspace / "cli-known"
    wt_foreign = audit_workspace / "cli-foreign"
    subprocess.run(
        ["git", "worktree", "add", str(wt_known), "-b", "known-branch"],
        cwd=clone, check=True, capture_output=True, env=env,
    )
    subprocess.run(
        ["git", "worktree", "add", str(wt_foreign), "-b", "foreign-branch"],
        cwd=clone, check=True, capture_output=True, env=env,
    )

    cfg = ConfigLoader.load(audit_workspace / "mothership.yaml")
    shell = ShellRunner()
    known = frozenset({wt_known.resolve()})
    rep = audit_repos(cfg, shell, names=["cli"], known_worktree_paths=known)

    cli_issues = next(r for r in rep.repos if r.name == "cli").issues
    extra = [i for i in cli_issues if i.code == "extra_worktrees"]
    assert len(extra) == 1
    assert "mship prune" in extra[0].message
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_repo_state.py -v -k "worktree_paths or foreign or known_worktree"`
Expected: FAIL — `_list_worktree_paths` not defined, `known_worktree_paths` not a kwarg yet, and the current `extra_worktrees` message doesn't mention `mship prune`.

- [ ] **Step 3: Implement `_list_worktree_paths` and thread the parameter**

In `src/mship/core/repo_state.py`:

Replace the existing extra-worktrees block in `_probe_git_wide` (currently around lines 150–159) with a call to the new helper. The full revised `_probe_git_wide` signature and body should read:

```python
def _list_worktree_paths(shell: ShellRunner, root_path: Path) -> list[Path]:
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
    shell: ShellRunner,
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
        rc, _, _ = _sh_out(shell, "git rev-parse --abbrev-ref --symbolic-full-name @{u}", root_path)
        if rc != 0:
            issues.append(Issue("no_upstream", "error", "current branch has no tracking remote"))
        else:
            rc_ah, out_ah, _ = _sh_out(shell, "git rev-list --count @{u}..HEAD", root_path)
            rc_be, out_be, _ = _sh_out(shell, "git rev-list --count HEAD..@{u}", root_path)
            if rc_ah == 0 and rc_be == 0:
                ahead = int(out_ah.strip() or "0")
                behind = int(out_be.strip() or "0")
                if ahead and behind:
                    issues.append(Issue("diverged", "error",
                                        f"{ahead} ahead, {behind} behind origin"))
                elif behind:
                    issues.append(Issue("behind_remote", "error",
                                        f"behind origin by {behind} commits"))
                elif ahead:
                    issues.append(Issue("ahead_remote", "info",
                                        f"ahead of origin by {ahead} commits"))

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
```

Then update `audit_repos` to accept and forward the parameter. In the current code at the bottom of the file, change the signature and the call to `_probe_git_wide`:

```python
def audit_repos(
    config: WorkspaceConfig,
    shell: ShellRunner,
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
                    "path_missing", "error", f"path not found: {root_path}"))
            continue
        if not (root_path / ".git").exists():
            for m in members:
                per_repo_issues[m].append(Issue(
                    "not_a_git_repo", "error", f"no .git at {root_path}"))
            continue

        root_cfg = config.repos.get(root_key)
        expected = (root_cfg.expected_branch if root_cfg is not None else None)
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

        for m in members:
            m_cfg = config.repos[m]
            subdir: Path | None = None
            if m_cfg.git_root is not None:
                subdir = m_cfg.path
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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_repo_state.py -v`
Expected: all pass — new tests plus the existing `test_audit_extra_worktrees` (which passes no `known_worktree_paths`, so default empty frozenset matches old behavior).

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q`
Expected: all passing. (Changes above are additive; all existing call sites still work with the default-empty `known_worktree_paths`.)

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/repo_state.py tests/core/test_repo_state.py
git commit -m "feat(audit): exclude mship-known worktrees from extra_worktrees check"
```

---

## Task 2: `collect_known_worktree_paths` helper + wire CLI call sites

**Files:**
- Modify: `src/mship/core/audit_gate.py`
- Modify: `src/mship/cli/audit.py`
- Modify: `src/mship/cli/sync.py`
- Modify: `src/mship/cli/worktree.py` (spawn + finish)
- Test: `tests/core/test_audit_gate.py`
- Test: `tests/cli/test_audit.py`
- Test: `tests/test_finish_integration.py`

- [ ] **Step 1: Write the failing unit test for the helper**

Append to `tests/core/test_audit_gate.py`:
```python
from pathlib import Path

from mship.core.audit_gate import collect_known_worktree_paths


class _FakeTask:
    def __init__(self, worktrees: dict[str, str]):
        self.worktrees = worktrees


class _FakeState:
    def __init__(self, tasks):
        self.tasks = tasks


class _FakeStateMgr:
    def __init__(self, state):
        self._state = state

    def load(self):
        return self._state


def test_collect_known_worktree_paths_union_across_tasks(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    state = _FakeState({
        "task1": _FakeTask({"cli": str(tmp_path / "a"), "api": str(tmp_path / "b")}),
        "task2": _FakeTask({"cli": str(tmp_path / "c")}),
    })
    result = collect_known_worktree_paths(_FakeStateMgr(state))
    expected = frozenset({
        (tmp_path / "a").resolve(),
        (tmp_path / "b").resolve(),
        (tmp_path / "c").resolve(),
    })
    assert result == expected


def test_collect_known_worktree_paths_no_tasks():
    state = _FakeState({})
    result = collect_known_worktree_paths(_FakeStateMgr(state))
    assert result == frozenset()
```

- [ ] **Step 2: Write the failing integration tests**

Append to `tests/cli/test_audit.py`:
```python
def test_audit_ignores_task_worktree(audit_workspace, tmp_path):
    """A worktree registered in state.tasks[*].worktrees is not flagged as extra."""
    import subprocess
    import os
    import yaml

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    # Add a git worktree to cli
    clone = audit_workspace / "cli"
    wt = audit_workspace / "cli-wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "scratch"],
        cwd=clone, check=True, capture_output=True, env=env,
    )

    # Register it in state as a task worktree
    state_dir = audit_workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_data = {
        "current_task": "t1",
        "tasks": {
            "t1": {
                "slug": "t1",
                "description": "t",
                "phase": "dev",
                "branch": "scratch",
                "affected_repos": ["cli"],
                "worktrees": {"cli": str(wt)},
                "pr_urls": {},
                "test_results": {},
            },
        },
    }
    (state_dir / "state.yaml").write_text(yaml.safe_dump(state_data))

    _override(audit_workspace)
    try:
        result = runner.invoke(app, ["audit", "--repos", "cli"])
        assert result.exit_code == 0, result.output
        assert "extra_worktrees" not in result.output
    finally:
        _reset()


def test_audit_still_flags_foreign_worktree(audit_workspace):
    """A worktree NOT in state still fires extra_worktrees."""
    import subprocess
    import os

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    clone = audit_workspace / "cli"
    wt = audit_workspace / "cli-foreign"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "foreign"],
        cwd=clone, check=True, capture_output=True, env=env,
    )

    _override(audit_workspace)
    try:
        result = runner.invoke(app, ["audit", "--repos", "cli"])
        assert result.exit_code == 1
        assert "extra_worktrees" in result.output
        assert "mship prune" in result.output
    finally:
        _reset()
```

Append to `tests/test_finish_integration.py`:
```python
def test_finish_not_blocked_by_own_worktree(finish_workspace):
    """mship finish must not block on extra_worktrees from its own worktree."""
    workspace, mock_shell = finish_workspace

    # Spawn normally (with --force-audit since the mock shell doesn't actually
    # make audits clean, the point of this test is what finish does).
    result = runner.invoke(app, ["spawn", "own wt", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    # Simulate audit returning extra_worktrees iff the worktree is not known.
    # Track the command and assert git worktree list is invoked; since
    # finish_workspace uses a shell mock, we stub its responses for audit probes.
    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "symbolic-ref" in cmd:
            return ShellResult(returncode=0, stdout="main\n", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git worktree list --porcelain" in cmd:
            # Report TWO worktrees: the main checkout AND the task's worktree.
            # The finish audit should exclude the task worktree.
            return ShellResult(
                returncode=0,
                stdout="worktree /tmp/shared\nworktree /tmp/shared-wt\n",
                stderr="",
            )
        if "git status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    # Point the state's worktree entry at /tmp/shared-wt so the exclusion matches
    # what the mocked `git worktree list` reports.
    state_path = workspace / ".mothership" / "state.yaml"
    import yaml
    data = yaml.safe_load(state_path.read_text())
    slug = data["current_task"]
    data["tasks"][slug]["worktrees"] = {"shared": "/tmp/shared-wt"}
    state_path.write_text(yaml.safe_dump(data))
    from mship.cli import container
    container.state_manager.reset()

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output
    assert "extra_worktrees" not in result.output
```

- [ ] **Step 3: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_audit_gate.py tests/cli/test_audit.py tests/test_finish_integration.py -v -k "known_worktree or ignores_task_worktree or foreign_worktree or not_blocked_by_own"`
Expected: FAIL — `collect_known_worktree_paths` doesn't exist, call sites aren't wired.

- [ ] **Step 4: Implement the helper**

In `src/mship/core/audit_gate.py`, add (at the end of the file, before any trailing blank):

```python
from pathlib import Path


def collect_known_worktree_paths(state_manager) -> "frozenset[Path]":
    """Return a resolved, absolute set of every worktree path in every task."""
    state = state_manager.load()
    paths: set[Path] = set()
    for task in state.tasks.values():
        for raw in task.worktrees.values():
            paths.add(Path(raw).resolve())
    return frozenset(paths)
```

(`from pathlib import Path` may already be imported at the top; if so, skip the duplicate import here and place `Path` inside the type hint string unchanged.)

- [ ] **Step 5: Wire the audit CLI**

In `src/mship/cli/audit.py`, modify the `audit` command body. Find the block that calls `audit_repos` and replace it:

```python
        try:
            from mship.core.audit_gate import collect_known_worktree_paths
            known = collect_known_worktree_paths(container.state_manager())
        except Exception:
            known = frozenset()

        try:
            report = audit_repos(config, shell, names=names, known_worktree_paths=known)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)
```

The try/except around `collect_known_worktree_paths` is defensive: if state doesn't exist yet in a fresh workspace, we still want `mship audit` to work (empty set → current behavior).

- [ ] **Step 6: Wire the sync CLI**

In `src/mship/cli/sync.py`, apply the same pattern. Find the `audit_repos(config, shell, names=names)` call and replace with:

```python
        try:
            from mship.core.audit_gate import collect_known_worktree_paths
            known = collect_known_worktree_paths(container.state_manager())
        except Exception:
            known = frozenset()

        try:
            report = audit_repos(config, shell, names=names, known_worktree_paths=known)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)
```

- [ ] **Step 7: Wire spawn gate**

In `src/mship/cli/worktree.py`, in the `spawn` command, find:
```python
        report = audit_repos(config, shell, names=audit_names)
```
Replace with:
```python
        from mship.core.audit_gate import collect_known_worktree_paths
        try:
            known = collect_known_worktree_paths(container.state_manager())
        except Exception:
            known = frozenset()
        report = audit_repos(config, shell, names=audit_names, known_worktree_paths=known)
```

- [ ] **Step 8: Wire finish gate**

In `src/mship/cli/worktree.py`, in the `finish` command, find:
```python
        report = audit_repos(config, shell, names=task.affected_repos)
```
Replace with:
```python
        from mship.core.audit_gate import collect_known_worktree_paths
        try:
            known = collect_known_worktree_paths(container.state_manager())
        except Exception:
            known = frozenset()
        report = audit_repos(config, shell, names=task.affected_repos, known_worktree_paths=known)
```

- [ ] **Step 9: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_audit_gate.py tests/cli/test_audit.py tests/test_finish_integration.py -v`
Expected: all pass — new tests plus any existing tests in those files.

- [ ] **Step 10: Full suite regression**

Run: `uv run pytest -q`
Expected: all passing. The `test_audit_extra_worktrees` test in `tests/core/test_repo_state.py` still passes because it doesn't register the worktree in any state — so it falls through as "not known" and still fires. Same for the rest of the existing suite.

- [ ] **Step 11: Commit**

```bash
git add src/mship/core/audit_gate.py src/mship/cli/audit.py src/mship/cli/sync.py \
        src/mship/cli/worktree.py tests/core/test_audit_gate.py tests/cli/test_audit.py \
        tests/test_finish_integration.py
git commit -m "feat(audit): thread known_worktree_paths through spawn/finish/audit/sync"
```

---

## Self-Review

**Spec coverage:**
- `audit_repos(..., known_worktree_paths=...)` signature + filter in `_probe_git_wide`: Task 1.
- `_list_worktree_paths` helper with resolved paths: Task 1.
- Refined message with `mship prune` hint: Task 1 (`_probe_git_wide` extra-worktrees block).
- `collect_known_worktree_paths(state_manager)` helper: Task 2.
- Wiring into `mship audit`, `mship sync`, spawn, finish: Task 2.
- Unit test for `_list_worktree_paths`: Task 1.
- Unit tests for filter (known suppresses; foreign fires; counts correctly): Task 1.
- Unit test for `collect_known_worktree_paths`: Task 2.
- Integration test: `mship finish` no longer blocks on own worktree: Task 2.
- Integration test: `mship audit` flags foreign worktree; hint mentions `mship prune`: Task 2.

**Placeholder scan:** none.

**Type consistency:**
- `known_worktree_paths: frozenset[Path]` with default `frozenset()` used uniformly.
- `_list_worktree_paths(shell, root_path) -> list[Path]` signature matches all callers.
- `collect_known_worktree_paths(state_manager) -> frozenset[Path]` matches all callers.

**Known deferrals (explicit in spec):**
- Separate `orphan_worktree` issue code.
- Auto-prune integration.
- Config-level disable knob (use existing `allow_extra_worktrees: true`).
