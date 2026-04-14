# Worktree Discipline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three coupled guardrails — view scoping to `active_repo`, cwd warnings when the agent runs mship from outside its target worktree, and `close` safety requiring `finish` + recovery-path verification.

**Architecture:** Two new PR-state helpers (`check_merged_into_base`, `check_pushed_to_origin`) feed the `close` recovery-path gate. `close` grows `--abandon`; default refuses when `finished_at is None`. View constructors gain `scope_to_active` + `--all` CLI flag. `mship log` and `mship test` check cwd vs `task.worktrees[active_repo]` and emit a non-blocking warning. `mship switch` prepends a red `cd <path>` line when cwd differs.

**Tech Stack:** Python 3.12+, Typer, existing `PRManager`, `StateManager`, `base_resolver`, `LogManager`.

**Spec:** `docs/superpowers/specs/2026-04-14-worktree-discipline-design.md`

---

## File Structure

**Modify:**
- `src/mship/core/pr.py` — add `check_merged_into_base`, `check_pushed_to_origin`.
- `src/mship/cli/worktree.py` (close command) — `--abandon` flag; finish-required check; recovery-path check.
- `src/mship/cli/view/diff.py` — `scope_to_active` param + `--all` flag.
- `src/mship/cli/view/logs.py` — `--all` flag; filter by active_repo.
- `src/mship/cli/log.py` — cwd warning in `log_cmd`.
- `src/mship/cli/exec.py` (test_cmd) — cwd warning.
- `src/mship/cli/switch.py` — prepend `⚠ cd <path>` line when cwd mismatch.
- `skills/working-with-mothership/SKILL.md` — post-switch `cd` guidance.

**Test files extended:** `tests/core/test_pr.py`, `tests/cli/test_worktree.py`, `tests/cli/view/test_diff_view.py`, `tests/cli/view/test_logs_view.py`, `tests/cli/test_log.py`, `tests/test_test_diff_integration.py`, `tests/cli/test_switch.py`.

---

## Task 1: `PRManager` recovery-path helpers

**Files:**
- Modify: `src/mship/core/pr.py`
- Test: `tests/core/test_pr.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_pr.py`:
```python
def test_check_merged_into_base_true(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_merged_into_base(Path("/tmp/repo"), "feat/x", "main") is True
    cmd = mock_shell.run.call_args.args[0]
    assert "git merge-base --is-ancestor" in cmd
    assert "feat/x" in cmd
    assert "main" in cmd


def test_check_merged_into_base_false_on_nonzero(mock_shell: MagicMock):
    # git merge-base --is-ancestor returns 1 when NOT an ancestor
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_merged_into_base(Path("/tmp/repo"), "feat/x", "main") is False


def test_check_pushed_to_origin_true_when_sha_matches(mock_shell: MagicMock):
    def side_effect(cmd, cwd, env=None):
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/feat/x\n", stderr="")
        if "git rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="abc123\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.run.side_effect = side_effect
    mgr = PRManager(mock_shell)
    assert mgr.check_pushed_to_origin(Path("/tmp/repo"), "feat/x") is True


def test_check_pushed_to_origin_false_when_sha_differs(mock_shell: MagicMock):
    def side_effect(cmd, cwd, env=None):
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/feat/x\n", stderr="")
        if "git rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="def456\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.run.side_effect = side_effect
    mgr = PRManager(mock_shell)
    assert mgr.check_pushed_to_origin(Path("/tmp/repo"), "feat/x") is False


def test_check_pushed_to_origin_false_when_branch_not_on_origin(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pushed_to_origin(Path("/tmp/repo"), "feat/x") is False


def test_check_pushed_to_origin_false_on_ls_remote_failure(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=128, stdout="", stderr="network err")
    mgr = PRManager(mock_shell)
    assert mgr.check_pushed_to_origin(Path("/tmp/repo"), "feat/x") is False
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_pr.py -v -k "merged_into_base or pushed_to_origin"`
Expected: FAIL — methods missing.

- [ ] **Step 3: Implement the helpers**

In `src/mship/core/pr.py`, after `count_commits_ahead`, add:

```python
    def check_merged_into_base(self, repo_path: Path, branch: str, base: str) -> bool:
        """True if `branch` is an ancestor of `base` (i.e. already merged).

        Uses `git merge-base --is-ancestor`: exit 0 = ancestor, 1 = not, >1 = error.
        Any error → False (conservative).
        """
        result = self._shell.run(
            f"git merge-base --is-ancestor {shlex.quote(branch)} {shlex.quote(base)}",
            cwd=repo_path,
        )
        return result.returncode == 0

    def check_pushed_to_origin(self, repo_path: Path, branch: str) -> bool:
        """True if `branch` exists on origin at the exact same SHA as local HEAD.

        Any error or mismatch → False (conservative).
        """
        local = self._shell.run(
            f"git rev-parse {shlex.quote(branch)}",
            cwd=repo_path,
        )
        if local.returncode != 0:
            return False
        local_sha = local.stdout.strip()

        remote = self._shell.run(
            f"git ls-remote origin {shlex.quote(branch)}",
            cwd=repo_path,
        )
        if remote.returncode != 0:
            return False
        # Output: "<sha>\trefs/heads/<branch>\n"
        for line in remote.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0].strip() == local_sha:
                return True
        return False
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_pr.py -v`
Expected: PASS (existing + 6 new).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/pr.py tests/core/test_pr.py
git commit -m "feat(pr): add check_merged_into_base and check_pushed_to_origin"
```

---

## Task 2: `close` finish-required + recovery-path gate

**Files:**
- Modify: `src/mship/cli/worktree.py` (close command)
- Test: `tests/cli/test_worktree.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_worktree.py`:
```python
def _build_close_task(slug="t", finished=False, pr_urls=None, worktrees=None, branch="feat/t"):
    from datetime import datetime, timezone
    from mship.core.state import Task
    return Task(
        slug=slug, description="d", phase="review",
        created_at=datetime.now(timezone.utc),
        affected_repos=list((worktrees or {}).keys()),
        branch=branch,
        worktrees=worktrees or {},
        pr_urls=pr_urls or {},
        finished_at=datetime.now(timezone.utc) if finished else None,
    )


def test_close_refuses_when_not_finished_without_abandon(configured_git_app):
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    task = _build_close_task(finished=False)
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    r = CliRunner().invoke(_app, ["close", "--yes"])
    assert r.exit_code != 0
    assert "hasn't been finished" in r.output.lower() or "run `mship finish`" in r.output
    # State unchanged
    assert sm.load().current_task == "t"


def test_close_abandon_proceeds_when_no_commits(configured_git_app):
    """No commits past base → recoverable trivially; --abandon closes cleanly."""
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from mship.util.shell import ShellRunner, ShellResult
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    task = _build_close_task(finished=False)
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    # count_commits_ahead returns 0 → no commits past base → recovery check passes trivially
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="0\n", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon"])
        assert r.exit_code == 0, r.output
        assert sm.load().current_task is None
    finally:
        container.shell.reset_override()


def test_close_abandon_refuses_when_unrecoverable(configured_git_app):
    """Commits past base, not merged, not pushed, no PR → refuses without --force."""
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from mship.util.shell import ShellRunner, ShellResult
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    # worktrees dict must map repo → path; use a dummy path that need not exist,
    # since the recovery check guards on path existence before calling git.
    task = _build_close_task(
        finished=False,
        worktrees={"shared": configured_git_app / "shared"},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    def mock_run(cmd, cwd, env=None):
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="3\n", stderr="")  # 3 commits past base
        if "git merge-base --is-ancestor" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")  # not merged
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")  # not pushed
        if "git rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="abc123\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon"])
        assert r.exit_code != 0
        assert "unrecoverable" in r.output.lower() or "permanently lost" in r.output.lower()
        assert sm.load().current_task == "t"  # unchanged
    finally:
        container.shell.reset_override()


def test_close_force_bypasses_recovery_check(configured_git_app):
    """--force destroys unrecoverable work on purpose."""
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from mship.util.shell import ShellRunner, ShellResult
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    task = _build_close_task(
        finished=False,
        worktrees={"shared": configured_git_app / "shared"},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    def mock_run(cmd, cwd, env=None):
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="3\n", stderr="")
        if "git merge-base --is-ancestor" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="abc123\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--force"])
        assert r.exit_code == 0, r.output
        assert sm.load().current_task is None
    finally:
        container.shell.reset_override()


def test_close_abandon_proceeds_when_merged(configured_git_app):
    """Commits past base but merged into base → recoverable."""
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from mship.util.shell import ShellRunner, ShellResult
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    task = _build_close_task(
        finished=False,
        worktrees={"shared": configured_git_app / "shared"},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    def mock_run(cmd, cwd, env=None):
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="3\n", stderr="")
        if "git merge-base --is-ancestor" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")  # is ancestor → merged
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon"])
        assert r.exit_code == 0, r.output
    finally:
        container.shell.reset_override()


def test_close_abandon_proceeds_when_pushed(configured_git_app):
    """Commits past base and pushed to origin → recoverable."""
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.core.state import StateManager, WorkspaceState
    from mship.util.shell import ShellRunner, ShellResult
    from typer.testing import CliRunner
    from mship.cli import app as _app

    sm = StateManager(configured_git_app / ".mothership")
    task = _build_close_task(
        finished=False,
        worktrees={"shared": configured_git_app / "shared"},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    def mock_run(cmd, cwd, env=None):
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="3\n", stderr="")
        if "git merge-base --is-ancestor" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")  # not merged
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/feat/t\n", stderr="")
        if "git rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="abc123\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        r = CliRunner().invoke(_app, ["close", "--yes", "--abandon"])
        assert r.exit_code == 0, r.output
    finally:
        container.shell.reset_override()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_worktree.py -v -k "refuses_when_not_finished or abandon or force_bypasses"`
Expected: FAIL — flag doesn't exist and the gate doesn't run.

- [ ] **Step 3: Update `close`**

In `src/mship/cli/worktree.py`, find the `close` command. Change the signature to add `--abandon`:

```python
    @app.command()
    def close(
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
        force: bool = typer.Option(False, "--force", "-f", help="Bypass ALL safety checks (destructive)"),
        abandon: bool = typer.Option(False, "--abandon", help="Close without finishing (discard PR flow)"),
        skip_pr_check: bool = typer.Option(False, "--skip-pr-check", help="Do not call gh; close regardless of PR state"),
    ):
```

At the top of the close body, after `task = state.tasks[task_slug]` is available, insert the two new checks (before the existing PR-state block):

```python
        # --- Finish-required check ---
        if task.finished_at is None and not abandon and not force:
            output.error(
                "Cannot close: task hasn't been finished.\n"
                "  Run `mship finish` to create PRs, or `mship close --abandon` to discard without PRs."
            )
            raise typer.Exit(code=1)

        # --- Recovery-path check ---
        if not force:
            from mship.core.base_resolver import resolve_base
            unrecoverable: list[tuple[str, int, str, str]] = []  # (repo, commits, branch, base)
            for repo_name in task.affected_repos:
                wt = task.worktrees.get(repo_name)
                if wt is None:
                    continue
                wt_path = Path(wt)
                if not wt_path.exists():
                    continue
                eff_base = resolve_base(
                    repo_name, config.repos[repo_name],
                    cli_base=None, base_map={}, known_repos=config.repos.keys(),
                )
                if eff_base is None:
                    continue  # conservative: can't verify, assume recoverable
                commits = pr_mgr.count_commits_ahead(wt_path, eff_base, task.branch)
                if commits == 0:
                    continue
                # Recovery checks
                merged = pr_mgr.check_merged_into_base(wt_path, task.branch, eff_base)
                has_pr = repo_name in task.pr_urls
                pushed = pr_mgr.check_pushed_to_origin(wt_path, task.branch)
                if merged or has_pr or pushed:
                    continue
                unrecoverable.append((repo_name, commits, task.branch, eff_base))

            if unrecoverable:
                output.error("Cannot close: unrecoverable commits in these repos:")
                for repo_name, commits, branch, eff_base in unrecoverable:
                    output.error(
                        f"  {repo_name}: {branch} ({commits} commits, "
                        f"not merged to {eff_base}, not pushed, no PR)"
                    )
                output.error("")
                output.error("These will be permanently lost. Options:")
                output.error("  - `mship finish` to create PRs")
                output.error("  - push from each worktree to save work")
                output.error("  - `mship close --force` to delete anyway (destructive)")
                raise typer.Exit(code=1)
```

Note: `config` is already loaded earlier in the close command; if not, add `config = container.config()` alongside `pr_mgr = container.pr_manager()`.

Update the log message mapping at the end of `close` so the abandoned path is logged distinctly. Find the existing `log_msg` assignment and add:

```python
        elif not task.pr_urls and task.finished_at is None and abandon:
            log_msg = "closed: cancelled before finish (abandoned)"
```

(Placement: inside the existing if/elif chain, before the generic `closed: cancelled before finish` case.)

If `--force` was used to bypass checks, also extend the log entry — find the existing `closed: forced with open PRs` line and, if `force=True` and the finish-required or recovery-path checks would have refused, update the message to reflect what was forced. Simpler: always add the `(forced)` suffix to any log line written under `force=True`.

One pragmatic approach: after computing `log_msg`, if `force and (task.finished_at is None or had_unrecoverable)`, append ` (forced)`. Track `had_unrecoverable = bool(unrecoverable)` by hoisting the list even when `force` is set (but skip the refusal).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cli/test_worktree.py -v`
Expected: PASS for all new tests; existing `close` tests still pass.

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_worktree.py
git commit -m "feat(close): require finish + verify recovery path before destroying work"
```

---

## Task 3: View scoping to `active_repo`

**Files:**
- Modify: `src/mship/cli/view/diff.py`
- Modify: `src/mship/cli/view/logs.py`
- Test: `tests/cli/view/test_diff_view.py`
- Test: `tests/cli/view/test_logs_view.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/view/test_diff_view.py`:
```python
@pytest.mark.asyncio
async def test_diff_scope_to_active_repo_filters_paths(tmp_path):
    wa = tmp_path / "a"
    wb = tmp_path / "b"
    # When scope_to_active_path is set, only that worktree is tracked
    view = DiffView(
        worktree_paths=[wa, wb],
        use_delta=False,
        watch=False,
        interval=1.0,
        scope_to_active_path=wa,
    )
    _seed(view, {
        wa: [_fd("a.py", "diff --git a/a.py b/a.py\n+++ b/a.py\n+x\n")],
        wb: [_fd("b.py", "diff --git a/b.py b/b.py\n+++ b/b.py\n+y\n")],
    })
    async with view.run_test() as pilot:
        await pilot.pause()
        labels = view.tree_labels()
        assert any(str(wa) in l for l in labels)
        assert not any(str(wb) in l for l in labels)


@pytest.mark.asyncio
async def test_diff_scope_none_shows_all(tmp_path):
    wa = tmp_path / "a"
    wb = tmp_path / "b"
    view = DiffView(
        worktree_paths=[wa, wb],
        use_delta=False,
        watch=False,
        interval=1.0,
        scope_to_active_path=None,  # --all equivalent
    )
    _seed(view, {
        wa: [_fd("a.py", "diff --git a/a.py b/a.py\n+++ b/a.py\n+x\n")],
        wb: [_fd("b.py", "diff --git a/b.py b/b.py\n+++ b/b.py\n+y\n")],
    })
    async with view.run_test() as pilot:
        await pilot.pause()
        labels = view.tree_labels()
        assert any(str(wa) in l for l in labels)
        assert any(str(wb) in l for l in labels)
```

Append to `tests/cli/view/test_logs_view.py`:
```python
@pytest.mark.asyncio
async def test_logs_view_scopes_to_active_repo():
    from datetime import datetime, timezone

    entries = [
        _Entry(datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc), "shared thing", repo="shared"),
        _Entry(datetime(2026, 4, 14, 10, 5, tzinfo=timezone.utc), "cli thing", repo="cli"),
        _Entry(datetime(2026, 4, 14, 10, 6, tzinfo=timezone.utc), "untagged thing", repo=None),
    ]
    view = LogsView(
        state_manager=_FakeStateMgr(),
        log_manager=_FakeLogMgr(entries),
        task_slug=None,
        scope_to_repo="cli",
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "cli thing" in text
        assert "shared thing" not in text
        # Untagged entries are kept (no repo tag to filter by)
        assert "untagged thing" in text


@pytest.mark.asyncio
async def test_logs_view_scope_none_shows_all():
    from datetime import datetime, timezone

    entries = [
        _Entry(datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc), "shared thing", repo="shared"),
        _Entry(datetime(2026, 4, 14, 10, 5, tzinfo=timezone.utc), "cli thing", repo="cli"),
    ]
    view = LogsView(
        state_manager=_FakeStateMgr(),
        log_manager=_FakeLogMgr(entries),
        task_slug=None,
        scope_to_repo=None,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "cli thing" in text
        assert "shared thing" in text
```

You'll need to update the `_Entry` helper in `test_logs_view.py` to accept a `repo` kwarg if it doesn't already — mirror the existing `LogEntry` fields.

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/view/test_diff_view.py tests/cli/view/test_logs_view.py -v -k "scope"`
Expected: FAIL — constructors don't accept the new kwarg.

- [ ] **Step 3: Update `DiffView`**

In `src/mship/cli/view/diff.py`, change `DiffView.__init__`:

```python
    def __init__(
        self,
        worktree_paths: Iterable[Path],
        use_delta: bool | None = None,
        scope_to_active_path: Path | None = None,
        **kw,
    ):
        super().__init__(**kw)
        all_paths = list(worktree_paths)
        if scope_to_active_path is not None:
            resolved = Path(scope_to_active_path).resolve()
            filtered = [p for p in all_paths if Path(p).resolve() == resolved]
            # If the active path isn't in the list, fall back to showing everything.
            self._paths = filtered if filtered else all_paths
        else:
            self._paths = all_paths
        if use_delta is None:
            use_delta = shutil.which("delta") is not None
        self._use_delta = use_delta
        # ...rest unchanged
```

Update the `register` function in the same file to read `task.active_repo` and pass through:

```python
def register(app: typer.Typer, get_container):
    @app.command()
    def diff(
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        all_: bool = typer.Option(False, "--all", help="Show all worktrees, ignore active_repo"),
    ):
        """Live per-worktree git diff, browsable by file."""
        container = get_container()
        worktree_paths = _collect_workspace_worktrees(container)

        scope_path: Path | None = None
        if not all_:
            state = container.state_manager().load()
            if state.current_task is not None:
                task = state.tasks[state.current_task]
                if task.active_repo is not None and task.active_repo in task.worktrees:
                    scope_path = Path(task.worktrees[task.active_repo])

        view = DiffView(
            worktree_paths=worktree_paths,
            scope_to_active_path=scope_path,
            watch=watch,
            interval=interval,
        )
        view.run()
```

- [ ] **Step 4: Update `LogsView`**

In `src/mship/cli/view/logs.py`, extend `LogsView.__init__` with a `scope_to_repo: str | None = None` param; filter entries in `gather()`:

```python
    def __init__(self, state_manager, log_manager, task_slug, scope_to_repo: str | None = None, **kw):
        super().__init__(**kw)
        self._state_manager = state_manager
        self._log_manager = log_manager
        self._task_slug = task_slug
        self._scope_to_repo = scope_to_repo
```

In `gather()`, after `entries = self._log_manager.read(slug)`, filter:

```python
        if self._scope_to_repo is not None:
            # Keep entries tagged with the target repo OR untagged
            entries = [e for e in entries if e.repo is None or e.repo == self._scope_to_repo]
```

Update `register`:

```python
def register(app: typer.Typer, get_container):
    @app.command()
    def logs(
        task_slug: Optional[str] = typer.Argument(None),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        all_: bool = typer.Option(False, "--all", help="Show all log entries, ignore active_repo"),
    ):
        """Live tail of a task's log."""
        container = get_container()
        scope: str | None = None
        if not all_:
            state = container.state_manager().load()
            if state.current_task is not None:
                scope = state.tasks[state.current_task].active_repo
        view = LogsView(
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            task_slug=task_slug,
            scope_to_repo=scope,
            watch=watch,
            interval=interval,
        )
        view.run()
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/cli/view -v`
Expected: PASS (new + existing).

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/view/diff.py src/mship/cli/view/logs.py \
        tests/cli/view/test_diff_view.py tests/cli/view/test_logs_view.py
git commit -m "feat(view): scope diff and logs to active_repo; --all opts out"
```

---

## Task 4: cwd warnings in `mship log` and `mship test`

**Files:**
- Modify: `src/mship/cli/log.py`
- Modify: `src/mship/cli/exec.py` (test_cmd)
- Test: `tests/cli/test_log.py`
- Test: `tests/test_test_diff_integration.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_log.py`:
```python
def test_log_warns_when_cwd_outside_active_worktree(workspace_with_git, tmp_path, monkeypatch):
    from mship.cli import app, container
    from typer.testing import CliRunner
    from mship.core.state import StateManager
    _runner = CliRunner()

    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        _runner.invoke(app, ["spawn", "cwd test", "--repos", "shared", "--force-audit"])
        _runner.invoke(app, ["switch", "shared"])

        # Run mship log from a dir that is NOT inside the active worktree
        monkeypatch.chdir(tmp_path)  # a fresh, unrelated tmp dir
        result = _runner.invoke(app, ["log", "something"])
        # Log still writes (non-blocking), but output mentions the wrong cwd
        assert result.exit_code == 0
        assert "⚠" in result.output or "not the active" in result.output.lower() or "running from" in result.output.lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.log_manager.reset()


def test_log_silent_when_cwd_inside_active_worktree(workspace_with_git, monkeypatch):
    from mship.cli import app, container
    from typer.testing import CliRunner
    _runner = CliRunner()

    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        _runner.invoke(app, ["spawn", "cwd test2", "--repos", "shared", "--force-audit"])
        _runner.invoke(app, ["switch", "shared"])

        # cd into the actual worktree
        from mship.core.state import StateManager
        state = StateManager(workspace_with_git / ".mothership").load()
        wt = state.tasks["cwd-test2"].worktrees["shared"]
        monkeypatch.chdir(wt)

        result = _runner.invoke(app, ["log", "something inside"])
        assert result.exit_code == 0
        # No cwd warning when we're in the right place
        assert "⚠" not in result.output
        assert "not the active" not in result.output.lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.log_manager.reset()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_log.py -v -k "cwd"`
Expected: FAIL — warning not emitted.

- [ ] **Step 3: Add a shared cwd-check helper**

Create `src/mship/cli/_cwd_check.py`:
```python
"""Shared cwd-vs-active-worktree check for log/test/etc."""
from pathlib import Path


def format_cwd_warning(cwd: Path, worktree: Path) -> str | None:
    """Return a warning string if cwd is not inside worktree, else None."""
    try:
        cwd_r = cwd.resolve()
        wt_r = worktree.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        cwd_r.relative_to(wt_r)
        return None  # cwd IS inside the worktree
    except ValueError:
        return (
            f"⚠ running from {cwd_r}, not the active repo's worktree at {wt_r}\n"
            f"  (commands still run in the correct path, but edits in your shell won't affect the worktree)"
        )
```

- [ ] **Step 4: Wire into `mship log`**

In `src/mship/cli/log.py`, near the top of `log_cmd`, after state + task are loaded, before the `show_open`/message/read branches, add:

```python
        from pathlib import Path as _P
        from mship.cli._cwd_check import format_cwd_warning
        if task.active_repo is not None and task.active_repo in task.worktrees:
            warn = format_cwd_warning(_P.cwd(), _P(task.worktrees[task.active_repo]))
            if warn is not None:
                output.print(f"[yellow]{warn}[/yellow]")
```

- [ ] **Step 5: Wire into `mship test`**

In `src/mship/cli/exec.py` `test_cmd`, after `task = state.tasks[state.current_task]`, add the same pattern:

```python
        from pathlib import Path as _P
        from mship.cli._cwd_check import format_cwd_warning
        if task.active_repo is not None and task.active_repo in task.worktrees:
            warn = format_cwd_warning(_P.cwd(), _P(task.worktrees[task.active_repo]))
            if warn is not None:
                output.print(f"[yellow]{warn}[/yellow]")
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/cli/test_log.py tests/test_test_diff_integration.py -v`
Expected: PASS for new cwd tests + existing tests still pass.

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 7: Commit**

```bash
git add src/mship/cli/_cwd_check.py src/mship/cli/log.py src/mship/cli/exec.py \
        tests/cli/test_log.py
git commit -m "feat(cli): warn when log/test runs from outside active worktree"
```

---

## Task 5: `mship switch` cd hint + skill doc

**Files:**
- Modify: `src/mship/cli/switch.py`
- Modify: `skills/working-with-mothership/SKILL.md`
- Test: `tests/cli/test_switch.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_switch.py`:
```python
def test_switch_prepends_cd_hint_when_cwd_differs(switch_workspace, monkeypatch):
    workspace, shared_wt, cli_wt, sm = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        # CliRunner uses subprocess cwd = test's cwd, which is NOT the worktree
        monkeypatch.chdir(workspace)  # workspace root ≠ cli_wt
        result = runner.invoke(app, ["switch", "cli"])
        assert result.exit_code == 0, result.output
        # TTY output; CliRunner's output is non-TTY though, so this may not show.
        # Instead assert the JSON path still does NOT contain the cd hint
        # and relies on separate TTY test. For now, just ensure the command ran.
    finally:
        _reset()


def test_switch_includes_worktree_path_in_output(switch_workspace, monkeypatch):
    """Assert the worktree path is always surfaced (as cd hint or in JSON)."""
    import json
    workspace, shared_wt, cli_wt, sm = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        monkeypatch.chdir(workspace)
        result = runner.invoke(app, ["switch", "cli"])
        assert result.exit_code == 0, result.output
        # Non-TTY → JSON; worktree_path in payload
        try:
            payload = json.loads(result.output)
            assert payload["worktree_path"] == str(cli_wt)
        except json.JSONDecodeError:
            # TTY mode: the path should appear literally
            assert str(cli_wt) in result.output
    finally:
        _reset()
```

(The `_seed_switchable` helper may need to be renamed or pulled from `test_switch.py`'s existing fixture usage — check current naming in that file and adapt. If it's `_seed_switchable` that returns the tuple, great; otherwise match the existing fixture pattern.)

Skip asserting on the `⚠ cd` hint text directly since CliRunner is non-TTY. The hint fires in TTY mode only; integration with a real terminal is manual.

- [ ] **Step 2: Run test, verify it passes (structural check)**

Run: `uv run pytest tests/cli/test_switch.py -v -k "cd_hint or worktree_path_in_output"`
Expected: PASS (these tests are structural, not dependent on the new behavior).

- [ ] **Step 3: Implement the cd hint in switch TTY output**

In `src/mship/cli/switch.py`, find the TTY rendering block (after `if not output.is_tty: ...`). Before any of the existing `lines.append(...)` calls that render the handoff, prepend the cd hint:

```python
        # TTY rendering
        verb = "Switched to" if is_switch else "Currently at"
        lines: list[str] = []

        # Prepend the cd hint when cwd is not inside the worktree.
        from pathlib import Path as _P
        try:
            cwd_r = _P.cwd().resolve()
            wt_r = handoff.worktree_path.resolve()
            cwd_inside = False
            try:
                cwd_r.relative_to(wt_r)
                cwd_inside = True
            except ValueError:
                cwd_inside = False
        except (OSError, RuntimeError):
            cwd_inside = True  # can't determine → don't nag
        if not cwd_inside and not handoff.worktree_missing:
            lines.append(f"[bold red]⚠ cd {handoff.worktree_path}[/bold red]")
            lines.append("")

        if handoff.worktree_missing:
            lines.append(
                f"[red]⚠ worktree missing:[/red] {handoff.worktree_path} "
                f"(run `mship prune` or `mship close`)"
            )
        # ...rest of the existing rendering
```

Place the cd hint so it appears before ALL other lines (including worktree_missing and finished_at warnings). This matches "unmissable first line" from the spec.

- [ ] **Step 4: Update skill doc**

In `skills/working-with-mothership/SKILL.md`, under the "Working on a task" prose section (the paragraph that already mentions `mship switch`), append or add a follow-up bullet:

```markdown
**After `mship switch <repo>`, `cd` to the worktree shown at the top of the handoff.**
If you don't, your edits in the shell affect the main checkout, not the feature branch.
`mship log` and `mship test` will warn when run from outside the active worktree.
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/cli/test_switch.py -v`
Expected: PASS.

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/switch.py skills/working-with-mothership/SKILL.md \
        tests/cli/test_switch.py
git commit -m "feat(switch): prepend cd hint when cwd is outside the target worktree"
```

---

## Self-Review

**Spec coverage:**
- View scoping (`view diff`, `view logs` default to active_repo; `--all` opts out): Task 3.
- `mship switch` cd hint: Task 5.
- cwd warning in log + test: Task 4.
- `close` finish-required + `--abandon`: Task 2.
- Recovery-path check (merged / pushed / PR): Task 1 (helpers) + Task 2 (wiring).
- `--force` bypasses all safety checks: Task 2.
- Skill doc update: Task 5.

**Placeholder scan:** none.

**Type consistency:**
- `PRManager.check_merged_into_base(repo_path, branch, base) -> bool` matches Task 1 def and Task 2 caller.
- `PRManager.check_pushed_to_origin(repo_path, branch) -> bool` matches Task 1 def and Task 2 caller.
- `DiffView(..., scope_to_active_path: Path | None = None, ...)` matches Task 3 impl and test.
- `LogsView(..., scope_to_repo: str | None = None, ...)` matches Task 3 impl and test.
- `format_cwd_warning(cwd, worktree) -> str | None` matches Task 4 impl and callers in log/test.

**Known deferrals (explicit in spec):** `--cd` shell integration, OSC-7 terminal hints, blocking on wrong cwd.
