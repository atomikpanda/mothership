# `finish` → `close` Lifecycle + Status Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename `abort`→`close` with smart PR-state detection, add `mship finish --push-only`, track `finished_at` on tasks with phase guardrails, and enrich `mship status` + `mship view status` with drift / last-log / phase-duration / finish signals.

**Architecture:** Extend `Task` model with two optional timestamp fields (`finished_at`, `phase_entered_at`). Stamp those fields from `PhaseManager.transition` and the `finish` command. Replace the `abort` command with `close`, which calls a new `PRManager.check_pr_state` helper and routes on the combined outcome. Status output gains four lines sourced from a new `audit_repos(local_only=True)` mode, the existing `LogManager`, and a new `format_relative` utility.

**Tech Stack:** Python 3.12+, Pydantic, Typer, `gh` CLI (for PR state), existing `ShellRunner`/`LogManager`/`StateManager`.

**Spec:** `docs/superpowers/specs/2026-04-13-finish-close-status-design.md`

---

## File Structure

**Create:**
- `src/mship/util/duration.py` — `format_relative(dt: datetime) -> str`.
- `tests/util/__init__.py` (if missing), `tests/util/test_duration.py`.

**Modify:**
- `src/mship/core/state.py` — add `finished_at`, `phase_entered_at` on `Task`.
- `src/mship/core/phase.py` — stamp `phase_entered_at`; refuse transitions on finished tasks (except `run` and `--force`).
- `src/mship/core/pr.py` — new `check_pr_state(pr_url) -> Literal["open","closed","merged","unknown"]`.
- `src/mship/core/repo_state.py` — `audit_repos(..., local_only: bool = False)`.
- `src/mship/cli/worktree.py` — `finish` stamps `finished_at`, adds `--push-only`; delete `abort`, add `close`.
- `src/mship/cli/phase.py` — pass `--force` through to `PhaseManager.transition` for the finished-task guardrail.
- `src/mship/cli/status.py` — emit drift/last-log/phase-duration/finished lines (TTY + JSON).
- `src/mship/cli/view/status.py` — same content in the TUI.
- `skills/working-with-mothership/SKILL.md`, `README.md` — rename `abort`→`close`, document `--push-only`, note finished-task behavior.

---

## Task 1: `Task` state fields + `format_relative` utility

**Files:**
- Modify: `src/mship/core/state.py` (Task class at line 15)
- Create: `src/mship/util/duration.py`
- Create: `tests/util/__init__.py` (empty) if it doesn't exist
- Test: `tests/util/test_duration.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/util/test_duration.py`:
```python
from datetime import datetime, timedelta, timezone

from mship.util.duration import format_relative


_NOW = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)


def test_just_now_for_future():
    result = format_relative(_NOW + timedelta(seconds=5), _now=_NOW)
    assert result == "just now"


def test_zero_seconds_is_just_now():
    assert format_relative(_NOW, _now=_NOW) == "just now"


def test_under_a_minute_shows_seconds():
    assert format_relative(_NOW - timedelta(seconds=30), _now=_NOW) == "30s ago"


def test_minutes():
    assert format_relative(_NOW - timedelta(minutes=5), _now=_NOW) == "5m ago"


def test_hours_and_minutes():
    assert format_relative(_NOW - timedelta(hours=3, minutes=12), _now=_NOW) == "3h 12m ago"


def test_hours_no_minutes():
    assert format_relative(_NOW - timedelta(hours=3), _now=_NOW) == "3h ago"


def test_days_and_hours():
    assert format_relative(_NOW - timedelta(days=2, hours=4), _now=_NOW) == "2d 4h ago"


def test_far_past():
    assert format_relative(_NOW - timedelta(days=45), _now=_NOW) == "30+ days ago"


def test_naive_datetime_treated_as_utc():
    # No tzinfo → interpret as UTC
    naive = datetime(2026, 4, 13, 11, 55, 0)
    assert format_relative(naive, _now=_NOW) == "5m ago"
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/util/test_duration.py -v`
Expected: FAIL — `mship.util.duration` missing.

- [ ] **Step 3: Add the state fields**

In `src/mship/core/state.py`, modify the `Task` class:
```python
class Task(BaseModel):
    slug: str
    description: str
    phase: Literal["plan", "dev", "review", "run"]
    created_at: datetime
    affected_repos: list[str]
    worktrees: dict[str, Path] = {}
    branch: str
    test_results: dict[str, TestResult] = {}
    blocked_reason: str | None = None
    blocked_at: datetime | None = None
    pr_urls: dict[str, str] = {}
    finished_at: datetime | None = None
    phase_entered_at: datetime | None = None
```

- [ ] **Step 4: Implement `format_relative`**

Create `src/mship/util/duration.py`:
```python
from datetime import datetime, timezone


def format_relative(dt: datetime, *, _now: datetime | None = None) -> str:
    """Return a short 'N ago' string for a datetime relative to now.

    Naive datetimes are interpreted as UTC. Future datetimes and zero-second
    deltas render as 'just now'. Deltas over 30 days render as '30+ days ago'.
    """
    now = _now if _now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    delta = now - dt
    total = int(delta.total_seconds())
    if total <= 0:
        return "just now"
    if total < 60:
        return f"{total}s ago"

    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m ago"

    hours, rem_min = divmod(minutes, 60)
    if hours < 24:
        if rem_min:
            return f"{hours}h {rem_min}m ago"
        return f"{hours}h ago"

    days, rem_hours = divmod(hours, 24)
    if days > 30:
        return "30+ days ago"
    if rem_hours:
        return f"{days}d {rem_hours}h ago"
    return f"{days}d ago"
```

If `tests/util/__init__.py` doesn't exist, create it as an empty file.

- [ ] **Step 5: Run tests, verify they pass**

Run: `uv run pytest tests/util/test_duration.py tests/core/test_state.py -v`
Expected: duration tests pass; state tests keep passing (new fields default to `None`, existing state files still parse).

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/state.py src/mship/util/duration.py \
        tests/util/__init__.py tests/util/test_duration.py
git commit -m "feat(state): add finished_at + phase_entered_at fields, duration util"
```

---

## Task 2: `PhaseManager` stamps `phase_entered_at` + finished-task guardrail

**Files:**
- Modify: `src/mship/core/phase.py`
- Modify: `src/mship/cli/phase.py` (pass-through `--force` into manager)
- Test: `tests/core/test_phase.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_phase.py`:
```python
from datetime import datetime, timedelta, timezone

import pytest

from mship.core.phase import PhaseManager, FinishedTaskError
from mship.core.state import StateManager, Task, WorkspaceState


@pytest.fixture
def phase_env(tmp_path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    sm = StateManager(state_dir=state_dir)

    class _FakeLog:
        def __init__(self):
            self.entries: list[tuple[str, str]] = []

        def append(self, slug, msg):
            self.entries.append((slug, msg))

    log = _FakeLog()
    pm = PhaseManager(state_manager=sm, log=log)

    state = WorkspaceState(
        current_task="t",
        tasks={
            "t": Task(
                slug="t", description="d", phase="plan",
                created_at=datetime.now(timezone.utc),
                affected_repos=["a"], branch="feat/t",
            ),
        },
    )
    sm.save(state)
    return sm, pm, log


def test_transition_stamps_phase_entered_at(phase_env):
    sm, pm, _ = phase_env
    before = datetime.now(timezone.utc)
    pm.transition("t", "dev")
    after = datetime.now(timezone.utc)
    task = sm.load().tasks["t"]
    assert task.phase_entered_at is not None
    assert before <= task.phase_entered_at <= after


def test_transition_on_finished_task_refused(phase_env):
    sm, pm, _ = phase_env
    state = sm.load()
    state.tasks["t"].finished_at = datetime.now(timezone.utc) - timedelta(hours=2)
    sm.save(state)
    with pytest.raises(FinishedTaskError):
        pm.transition("t", "dev")


def test_transition_on_finished_task_allowed_with_force(phase_env):
    sm, pm, _ = phase_env
    state = sm.load()
    state.tasks["t"].finished_at = datetime.now(timezone.utc) - timedelta(hours=2)
    sm.save(state)
    result = pm.transition("t", "dev", force_finished=True)
    assert result.new_phase == "dev"
    # Warning surfaced explaining the override
    assert any("finished" in w.lower() for w in result.warnings)


def test_transition_to_run_on_finished_task_allowed_without_force(phase_env):
    sm, pm, _ = phase_env
    state = sm.load()
    state.tasks["t"].phase = "review"
    state.tasks["t"].finished_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    sm.save(state)
    result = pm.transition("t", "run")
    assert result.new_phase == "run"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_phase.py -v -k "stamps or finished"`
Expected: FAIL — `FinishedTaskError` missing, `force_finished` kwarg missing, `phase_entered_at` not stamped.

- [ ] **Step 3: Implement the changes in `PhaseManager`**

Replace `src/mship/core/phase.py` body (keep the dataclass + constants, replace `PhaseManager`):

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from mship.core.log import LogManager
from mship.core.state import StateManager

Phase = Literal["plan", "dev", "review", "run"]
PHASE_ORDER: list[Phase] = ["plan", "dev", "review", "run"]


class FinishedTaskError(RuntimeError):
    """Raised when transitioning a finished task to plan/dev/review without --force."""


@dataclass
class PhaseTransition:
    new_phase: Phase
    warnings: list[str] = field(default_factory=list)


class PhaseManager:
    """Manages phase transitions with soft gates."""

    def __init__(self, state_manager: StateManager, log: LogManager) -> None:
        self._state_manager = state_manager
        self._log = log

    def transition(
        self,
        task_slug: str,
        target: Phase,
        force_unblock: bool = False,
        force_finished: bool = False,
    ) -> PhaseTransition:
        state = self._state_manager.load()
        task = state.tasks[task_slug]

        # Finished-task guardrail: plan/dev/review refuse; run is always allowed.
        if task.finished_at is not None and target != "run" and not force_finished:
            raise FinishedTaskError(
                f"Task '{task_slug}' is finished. Transitioning to {target} "
                f"probably means you want `mship close` then `mship spawn` for "
                f"the next task. Use --force to override."
            )

        old_phase = task.phase
        warnings = self._check_gates(task_slug, task.phase, target)

        if task.blocked_reason is not None and force_unblock:
            warnings.append(
                f"Task was blocked: {task.blocked_reason} — force-unblocked by phase transition"
            )
            self._log.append(
                task_slug,
                f"Unblocked (forced phase transition to {target})",
            )
            task.blocked_reason = None
            task.blocked_at = None

        if task.finished_at is not None and force_finished and target != "run":
            warnings.append(
                f"Task was finished (at {task.finished_at.isoformat()}) — "
                f"forced transition to {target}"
            )

        task.phase = target
        task.phase_entered_at = datetime.now(timezone.utc)
        self._state_manager.save(state)

        self._log.append(task_slug, f"Phase transition: {old_phase} → {target}")

        return PhaseTransition(new_phase=target, warnings=warnings)

    def _check_gates(
        self, task_slug: str, current: Phase, target: Phase
    ) -> list[str]:
        current_idx = PHASE_ORDER.index(current)
        target_idx = PHASE_ORDER.index(target)

        if target_idx <= current_idx:
            return []

        warnings: list[str] = []
        state = self._state_manager.load()
        task = state.tasks[task_slug]

        if target == "dev":
            warnings.extend(self._gate_dev(task_slug))
        elif target == "review":
            warnings.extend(self._gate_review(task))
        elif target == "run":
            warnings.extend(self._gate_run(task))

        return warnings

    def _gate_dev(self, task_slug: str) -> list[str]:
        return ["No spec found — consider writing one before developing"]

    def _gate_review(self, task) -> list[str]:
        warnings: list[str] = []
        missing = []
        failing = []
        for repo in task.affected_repos:
            result = task.test_results.get(repo)
            if result is None:
                missing.append(repo)
            elif result.status == "fail":
                failing.append(repo)
        if missing:
            warnings.append(
                f"Tests not run in: {', '.join(missing)} — consider running tests before review"
            )
        if failing:
            warnings.append(
                f"Tests not passing in: {', '.join(failing)} — consider fixing before review"
            )
        return warnings

    def _gate_run(self, task) -> list[str]:
        return []
```

- [ ] **Step 4: Wire `--force` through the CLI**

In `src/mship/cli/phase.py`, find where `PhaseManager.transition` is called. Update the call to pass `force_finished=force`, and catch `FinishedTaskError` to exit 1 with a friendly message:

```python
@app.command()
def phase(
    target: Phase,
    force: bool = typer.Option(False, "--force", "-f", help="Force transition (clears block or finished-task guardrail)"),
):
    container = get_container()
    output = Output()
    state_mgr = container.state_manager()
    state = state_mgr.load()
    if state.current_task is None:
        output.error("No active task. Run `mship spawn` to start one.")
        raise typer.Exit(code=1)

    pm = container.phase_manager()
    from mship.core.phase import FinishedTaskError
    try:
        result = pm.transition(
            state.current_task,
            target,
            force_unblock=force,
            force_finished=force,
        )
    except FinishedTaskError as e:
        output.error(str(e))
        raise typer.Exit(code=1)

    # ...existing warnings/output code preserved below...
```

Keep whatever output logic already exists after the transition call. Only the `transition()` invocation and the `FinishedTaskError` catch are new.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/core/test_phase.py tests/cli/ -v`
Expected: new phase tests pass; existing phase + CLI tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/phase.py src/mship/cli/phase.py tests/core/test_phase.py
git commit -m "feat(phase): stamp phase_entered_at and guard finished-task transitions"
```

---

## Task 3: `PRManager.check_pr_state` + `audit_repos(local_only=True)`

**Files:**
- Modify: `src/mship/core/pr.py`
- Modify: `src/mship/core/repo_state.py`
- Test: `tests/core/test_pr.py`
- Test: `tests/core/test_repo_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_pr.py`:
```python
def test_check_pr_state_merged(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="MERGED\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://github.com/o/r/pull/1") == "merged"
    cmd = mock_shell.run.call_args.args[0]
    assert "gh pr view" in cmd
    assert "--json state" in cmd


def test_check_pr_state_closed(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="CLOSED\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1") == "closed"


def test_check_pr_state_open(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="OPEN\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1") == "open"


def test_check_pr_state_unknown_on_failure(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="not found")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1") == "unknown"


def test_check_pr_state_unknown_on_unexpected_output(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="DRAFT\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1") == "unknown"
```

Append to `tests/core/test_repo_state.py`:
```python
def test_audit_local_only_skips_fetch(audit_workspace):
    """local_only=True must not invoke git fetch."""
    import os, subprocess
    from mship.core.config import ConfigLoader
    from mship.core.repo_state import audit_repos
    from mship.util.shell import ShellRunner

    cfg = ConfigLoader.load(audit_workspace / "mothership.yaml")
    shell = ShellRunner()

    calls: list[str] = []
    real_run = shell.run

    def counting(cmd, cwd, env=None):
        calls.append(cmd)
        return real_run(cmd, cwd, env=env)

    shell.run = counting  # type: ignore[assignment]

    audit_repos(cfg, shell, names=["cli"], local_only=True)
    assert not any(c.startswith("git fetch") for c in calls)


def test_audit_local_only_still_detects_dirty(audit_workspace):
    """Cheap local checks still fire in local_only mode."""
    from mship.core.config import ConfigLoader
    from mship.core.repo_state import audit_repos
    from mship.util.shell import ShellRunner

    (audit_workspace / "cli" / "new.txt").write_text("x\n")

    cfg = ConfigLoader.load(audit_workspace / "mothership.yaml")
    shell = ShellRunner()
    rep = audit_repos(cfg, shell, names=["cli"], local_only=True)
    codes = {i.code for r in rep.repos if r.name == "cli" for i in r.issues}
    assert "dirty_worktree" in codes


def test_audit_local_only_does_not_emit_behind_or_fetch_failed(audit_workspace):
    """Even if tracking is broken, local_only audit never emits fetch-family codes."""
    import os, subprocess
    from mship.core.config import ConfigLoader
    from mship.core.repo_state import audit_repos
    from mship.util.shell import ShellRunner

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    # Break the remote to make fetch fail
    subprocess.run(
        ["git", "-C", str(audit_workspace / "cli"), "remote", "set-url", "origin", "/no/such.git"],
        check=True, capture_output=True, env=env,
    )

    cfg = ConfigLoader.load(audit_workspace / "mothership.yaml")
    shell = ShellRunner()
    rep = audit_repos(cfg, shell, names=["cli"], local_only=True)
    codes = {i.code for r in rep.repos if r.name == "cli" for i in r.issues}
    fetch_family = {"fetch_failed", "behind_remote", "ahead_remote", "diverged", "no_upstream"}
    assert not (codes & fetch_family), f"unexpected fetch-family codes: {codes & fetch_family}"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_pr.py tests/core/test_repo_state.py -v -k "check_pr_state or local_only"`
Expected: FAIL — methods/params missing.

- [ ] **Step 3: Implement `check_pr_state`**

In `src/mship/core/pr.py`, add the method on `PRManager` (placement: just after `verify_base_exists`):

```python
    def check_pr_state(self, pr_url: str) -> "str":
        """Return 'merged', 'closed', 'open', or 'unknown' for a PR URL.

        Uses `gh pr view --json state`. Any failure returns 'unknown'.
        """
        result = self._shell.run(
            f"gh pr view {shlex.quote(pr_url)} --json state -q .state",
            cwd=Path("."),
        )
        if result.returncode != 0:
            return "unknown"
        raw = result.stdout.strip().upper()
        mapping = {"MERGED": "merged", "CLOSED": "closed", "OPEN": "open"}
        return mapping.get(raw, "unknown")
```

- [ ] **Step 4: Implement `local_only` in audit**

In `src/mship/core/repo_state.py`, change the signatures and skip the fetch-family block when `local_only=True`.

First, update `_probe_git_wide` signature and body. Locate the current `_probe_git_wide` and replace it with:

```python
def _probe_git_wide(
    shell: ShellRunner,
    root_path: Path,
    expected_branch: str | None,
    allow_extra_worktrees: bool,
    known_worktree_paths: frozenset[Path],
    local_only: bool = False,
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

    if not local_only:
        rc, _, err = _sh_out(shell, "git fetch --prune origin", root_path)
        fetch_ok = rc == 0
        if not fetch_ok:
            issues.append(Issue(
                "fetch_failed", "error",
                err.strip().splitlines()[-1] if err.strip() else "fetch failed",
            ))

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

Next, thread `local_only` through `audit_repos`. Locate the function signature and the call to `_probe_git_wide`:

```python
def audit_repos(
    config: WorkspaceConfig,
    shell: ShellRunner,
    names: Iterable[str] | None = None,
    known_worktree_paths: frozenset[Path] = frozenset(),
    local_only: bool = False,
) -> AuditReport:
```

Inside the function, change the `_probe_git_wide` call to pass `local_only=local_only`:
```python
        current_branch, wide_issues = _probe_git_wide(
            shell, root_path, expected, allow_wt, known_worktree_paths, local_only,
        )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/core/test_pr.py tests/core/test_repo_state.py -v`
Expected: all pass — new tests + existing tests (default `local_only=False` preserves today's behavior).

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/pr.py src/mship/core/repo_state.py \
        tests/core/test_pr.py tests/core/test_repo_state.py
git commit -m "feat: add PRManager.check_pr_state and audit_repos local_only mode"
```

---

## Task 4: `mship finish` stamps `finished_at` + adds `--push-only`

**Files:**
- Modify: `src/mship/cli/worktree.py` (finish command around line 144)
- Test: `tests/test_finish_integration.py`

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/test_finish_integration.py`:
```python
def test_finish_stamps_finished_at(finish_workspace):
    workspace, mock_shell = finish_workspace

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        if "git rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["spawn", "stamp test", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output
    assert "mship close" in result.output

    from mship.core.state import StateManager
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["stamp-test"].finished_at is not None


def test_finish_push_only_skips_gh_pr_create(finish_workspace):
    workspace, mock_shell = finish_workspace
    push_calls: list[str] = []
    pr_calls: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "gh pr create" in cmd:
            pr_calls.append(cmd)
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        if "git push" in cmd:
            push_calls.append(cmd)
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["spawn", "push only", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--push-only"])
    assert result.exit_code == 0, result.output
    assert len(push_calls) == 1
    assert pr_calls == []
    assert "mship close" in result.output
    assert "Branch pushed" in result.output

    from mship.core.state import StateManager
    state = StateManager(workspace / ".mothership").load()
    task = state.tasks["push-only"]
    assert task.finished_at is not None
    assert task.pr_urls == {}


def test_finish_push_only_rejects_base_flags(finish_workspace):
    result = runner.invoke(app, ["spawn", "conflict flags", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0

    result = runner.invoke(app, ["finish", "--push-only", "--base", "main"])
    assert result.exit_code != 0
    assert "push-only" in result.output.lower()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_finish_integration.py -v -k "stamps or push_only or conflict"`
Expected: FAIL.

- [ ] **Step 3: Extend the `finish` command**

In `src/mship/cli/worktree.py`, in the `finish` command:

1. Add `--push-only` option to the signature:
```python
    def finish(
        handoff: bool = typer.Option(False, "--handoff", help="Generate CI handoff manifest"),
        base: Optional[str] = typer.Option(None, "--base", help="Global override of PR base branch for all repos"),
        base_map: Optional[str] = typer.Option(None, "--base-map", help="Per-repo PR base overrides"),
        force_audit: bool = typer.Option(False, "--force-audit", help="Bypass audit gate for this finish"),
        push_only: bool = typer.Option(False, "--push-only", help="Push branches only; skip gh pr create"),
    ):
```

2. Immediately after the signature and docstring, before loading state, add the flag-compatibility check:
```python
        if push_only and (handoff or base is not None or base_map is not None):
            output = Output()
            output.error("--push-only is incompatible with --handoff/--base/--base-map")
            raise typer.Exit(code=1)
```

3. Find the block that sets `pr_list` and handles PR creation (the `for i, repo_name in enumerate(ordered, 1):` loop). Replace its final state save path and nudge with the new stamping logic. At the very end of the function (after `pr_list` is complete, before any JSON output), insert:
```python
        # Stamp finished_at on successful completion.
        from datetime import datetime as _dt, timezone as _tz
        if task.finished_at is None:
            task.finished_at = _dt.now(_tz.utc)
            state_mgr.save(state)

        if output.is_tty:
            if push_only:
                output.print("[green]Branch pushed.[/green] After merge/review, run `mship close` to clean up.")
            else:
                output.print("[green]Task finished.[/green] After merge, run `mship close` to clean up.")
```

4. For `--push-only`, short-circuit the PR-creation loop. Before the existing `pr_mgr.check_gh_available()` call, branch:
```python
        if push_only:
            # Push-only path: skip gh entirely. Still run commits-ahead pre-flight
            # by reusing the existing audit-and-verify scaffold.
            pr_list: list[dict] = []
            for repo_name in ordered:
                if repo_name in task.pr_urls:
                    continue  # nothing to push for already-done repos
                repo_config = config.repos[repo_name]
                repo_path = repo_config.path
                if repo_name in task.worktrees:
                    wt_path = Path(task.worktrees[repo_name])
                    if wt_path.exists():
                        repo_path = wt_path
                # Ensure the branch actually has commits to push (same guard as PR flow)
                # Uses origin/HEAD (gh's default base) as the comparison — best-effort.
                # If we can't compute it, still push; git will no-op if branch matches origin.
                try:
                    pr_mgr.push_branch(repo_path, task.branch)
                except RuntimeError as e:
                    output.error(f"{repo_name}: {e}")
                    raise typer.Exit(code=1)
                if output.is_tty:
                    output.print(f"  {repo_name}: {task.branch} pushed")
                pr_list.append({"repo": repo_name, "branch": task.branch, "pushed": True})

            from datetime import datetime as _dt, timezone as _tz
            if task.finished_at is None:
                task.finished_at = _dt.now(_tz.utc)
                state_mgr.save(state)

            if output.is_tty:
                output.print("[green]Branch pushed.[/green] After merge/review, run `mship close` to clean up.")
            else:
                output.json({"task": task.slug, "pushed": [p["repo"] for p in pr_list], "finished_at": task.finished_at.isoformat()})
            return
```

(Place this block **after** `pr_mgr = container.pr_manager()` is instantiated but **before** `pr_mgr.check_gh_available()`, so `pr_mgr` exists but gh isn't probed.)

5. Make sure existing `output = Output()` is available above the new flag-compatibility check; if not, move the line earlier so both the flag check and the new nudge can use it.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_finish_integration.py -v`
Expected: all pass — new tests plus existing tests (defaults preserve today's behavior; stamping happens once on success).

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/worktree.py tests/test_finish_integration.py
git commit -m "feat(finish): stamp finished_at and add --push-only for non-PR flows"
```

---

## Task 5: `mship close` replaces `mship abort`

**Files:**
- Modify: `src/mship/cli/worktree.py` (remove `abort`, add `close`)
- Modify: `tests/cli/test_worktree.py` (rename `test_abort` → `test_close` variants)
- Modify: `tests/core/test_worktree.py` (preserve `wt_mgr.abort` test naming — the core method name is kept; only the CLI verb changes)
- Test: `tests/cli/test_worktree.py` — new close-specific cases

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_worktree.py` (or replace the existing `test_abort` block):

```python
def test_close_with_all_merged_prs(configured_git_app):
    # Seed state with a task that has pr_urls
    from mship.cli import container
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="review",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/t",
            pr_urls={"shared": "https://github.com/o/r/pull/1"},
            finished_at=datetime.now(timezone.utc),
        )},
    )
    sm.save(state)

    from unittest.mock import MagicMock
    from mship.util.shell import ShellRunner, ShellResult
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="MERGED\n", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        from typer.testing import CliRunner
        from mship.cli import app as _app
        r = CliRunner()
        result = r.invoke(_app, ["close", "--yes"])
        assert result.exit_code == 0, result.output
        assert "completed" in result.output.lower() or "merged" in result.output.lower()
        assert sm.load().current_task is None
    finally:
        container.shell.reset_override()


def test_close_with_open_pr_refuses_without_force(configured_git_app):
    from mship.cli import container
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="review",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/t",
            pr_urls={"shared": "https://x/1"},
            finished_at=datetime.now(timezone.utc),
        )},
    )
    sm.save(state)

    from unittest.mock import MagicMock
    from mship.util.shell import ShellRunner, ShellResult
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="OPEN\n", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        from typer.testing import CliRunner
        from mship.cli import app as _app
        r = CliRunner()
        result = r.invoke(_app, ["close", "--yes"])
        assert result.exit_code != 0
        assert "open" in result.output.lower()
        # State unchanged
        assert sm.load().current_task == "t"
    finally:
        container.shell.reset_override()


def test_close_with_open_pr_proceeds_under_force(configured_git_app):
    from mship.cli import container
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="review",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/t",
            pr_urls={"shared": "https://x/1"},
            finished_at=datetime.now(timezone.utc),
        )},
    )
    sm.save(state)

    from unittest.mock import MagicMock
    from mship.util.shell import ShellRunner, ShellResult
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="OPEN\n", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        from typer.testing import CliRunner
        from mship.cli import app as _app
        r = CliRunner()
        result = r.invoke(_app, ["close", "--yes", "--force"])
        assert result.exit_code == 0, result.output
        assert sm.load().current_task is None
    finally:
        container.shell.reset_override()


def test_close_with_no_prs_cancelled_before_finish(configured_git_app):
    from mship.cli import container
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    sm = StateManager(configured_git_app / ".mothership")
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="dev",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/t",
        )},
    )
    sm.save(state)

    from typer.testing import CliRunner
    from mship.cli import app as _app
    r = CliRunner()
    result = r.invoke(_app, ["close", "--yes"])
    assert result.exit_code == 0, result.output
    assert sm.load().current_task is None


def test_close_no_active_task_errors():
    from typer.testing import CliRunner
    from mship.cli import app as _app
    r = CliRunner()
    result = r.invoke(_app, ["close", "--yes"])
    # Either No active task, or config-not-found — both exit 1.
    assert result.exit_code != 0
```

If the existing `test_abort` test is present in `tests/cli/test_worktree.py`, delete it — `close` replaces `abort` at the CLI layer.

Note: `tests/core/test_worktree.py` tests exercise `WorktreeManager.abort` (the core method). Keep those as-is — we're not renaming the core method.

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_worktree.py -v -k "close or abort"`
Expected: FAIL — `close` command not defined; if the old `test_abort` is still present, it fails because the command was removed.

- [ ] **Step 3: Replace `abort` with `close`**

In `src/mship/cli/worktree.py`, delete the entire `abort` command definition (the `@app.command()` decorator and the `def abort(...)` function body) and add `close`:

```python
    @app.command()
    def close(
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
        force: bool = typer.Option(False, "--force", "-f", help="Close even with open PRs"),
        skip_pr_check: bool = typer.Option(False, "--skip-pr-check", help="Do not call gh; close regardless of PR state"),
    ):
        """Close the current task: check PR state, tear down worktrees, clear state."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task to close. Run `mship spawn` to start one.")
            raise typer.Exit(code=1)

        task_slug = state.current_task
        task = state.tasks[task_slug]
        pr_mgr = container.pr_manager()
        log_mgr = container.log_manager()

        # Determine the log message based on PR state.
        pr_states: list[str] = []  # parallel to task.pr_urls values
        if task.pr_urls and not skip_pr_check:
            import shutil
            if shutil.which("gh") is None and not force:
                output.error(
                    "gh CLI needed to check PR state. Install gh, or pass --skip-pr-check."
                )
                raise typer.Exit(code=1)
            for url in task.pr_urls.values():
                pr_states.append(pr_mgr.check_pr_state(url))

        # Route on PR states
        open_count = sum(1 for s in pr_states if s == "open")
        merged_count = sum(1 for s in pr_states if s == "merged")
        closed_count = sum(1 for s in pr_states if s == "closed")

        if task.pr_urls and skip_pr_check:
            log_msg = "closed: pr state unchecked"
        elif not task.pr_urls:
            if task.finished_at is not None:
                log_msg = "closed: no PRs (pushed via --push-only)"
            else:
                log_msg = "closed: cancelled before finish"
        elif open_count and not force:
            output.error(
                f"Task '{task_slug}' has {open_count} open PR(s). Merge or close them first, "
                f"or pass --force to override."
            )
            raise typer.Exit(code=1)
        elif open_count and force:
            log_msg = f"closed: forced with open PRs ({open_count} open)"
        elif merged_count and not closed_count:
            log_msg = f"closed: completed ({merged_count} PRs merged)"
        elif closed_count and not merged_count:
            log_msg = "closed: cancelled on GitHub"
        elif merged_count and closed_count:
            log_msg = f"closed: mixed ({merged_count} merged, {closed_count} closed)"
        else:
            log_msg = "closed: pr state unknown"

        if not yes and output.is_tty:
            from InquirerPy import inquirer
            confirm = inquirer.confirm(
                message=f"Close task '{task_slug}'? This will remove all worktrees.",
                default=False,
            ).execute()
            if not confirm:
                output.print("Cancelled")
                raise typer.Exit(code=0)

        wt_mgr = container.worktree_manager()
        wt_mgr.abort(task_slug)  # core method retains the name; only CLI verb changed
        log_mgr.append(task_slug, log_msg)
        output.success(f"{log_msg.capitalize()}: {task_slug}")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cli/test_worktree.py tests/core/test_worktree.py -v`
Expected: new tests pass; core-layer abort tests still pass (the `WorktreeManager.abort` method is untouched).

Then: `uv run pytest -q`
Expected: full suite passes.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_worktree.py
git commit -m "feat: replace mship abort with mship close (smart PR-state detection)"
```

---

## Task 6: Enriched `mship status` output (TTY + JSON + TUI)

**Files:**
- Modify: `src/mship/cli/status.py`
- Modify: `src/mship/cli/view/status.py`
- Test: `tests/cli/test_status.py` (create if absent)
- Test: `tests/cli/view/test_status_view.py`

- [ ] **Step 1: Write the failing tests**

Create or append `tests/cli/test_status.py`:

```python
import json
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, TestResult, WorkspaceState


runner = CliRunner()


def _seed(path, task: Task):
    sm = StateManager(path / ".mothership")
    sm.save(WorkspaceState(current_task=task.slug, tasks={task.slug: task}))


def test_status_shows_phase_duration_and_drift(workspace_with_git):
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
        phase_entered_at=datetime.now(timezone.utc) - timedelta(hours=3),
        affected_repos=["shared"], branch="feat/t",
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "3h" in result.output  # phase duration
        assert "Drift:" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_status_json_includes_new_fields(workspace_with_git):
    task = Task(
        slug="t", description="d", phase="review",
        created_at=datetime.now(timezone.utc),
        phase_entered_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        affected_repos=["shared"], branch="feat/t",
        finished_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        # Non-TTY → JSON
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["finished_at"] is not None
        assert payload["phase_entered_at"] is not None
        assert "drift" in payload
        assert "last_log" in payload
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_status_shows_finished_warning(workspace_with_git):
    task = Task(
        slug="t", description="d", phase="review",
        created_at=datetime.now(timezone.utc),
        phase_entered_at=datetime.now(timezone.utc),
        affected_repos=["shared"], branch="feat/t",
        finished_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "Finished" in result.output or "finished" in result.output
        assert "mship close" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
```

Append to `tests/cli/view/test_status_view.py`:

```python
@pytest.mark.asyncio
async def test_status_view_shows_finished_warning():
    from datetime import datetime, timezone, timedelta

    class _Task:
        slug = "t"
        phase = "review"
        phase_entered_at = datetime.now(timezone.utc) - timedelta(hours=1)
        blocked_reason = None
        blocked_at = None
        branch = "feat/t"
        affected_repos = ["r"]
        worktrees = {}
        test_results = {}
        pr_urls = {}
        finished_at = datetime.now(timezone.utc) - timedelta(hours=2)

    class _State:
        current_task = "t"
        tasks = {"t": _Task()}

    class _Mgr:
        def load(self):
            return _State()

    view = StatusView(state_manager=_Mgr(), watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "Finished" in text or "finished" in text
        assert "mship close" in text
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_status.py tests/cli/view/test_status_view.py -v`
Expected: new tests fail.

- [ ] **Step 3: Extend `cli/status.py`**

Replace the body of the `status` command. Keep the module-level imports; update the function. Full replacement for the status command:

```python
import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def status():
        """Show current phase, active task, worktrees, test results, drift, and recent activity."""
        from datetime import datetime, timezone
        from mship.util.duration import format_relative

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.print("No active task")
            if not output.is_tty:
                output.json({"current_task": None, "tasks": {}})
            return

        task = state.tasks[state.current_task]

        # Drift (local-only)
        drift_summary: dict = {"has_errors": False, "error_count": 0}
        try:
            from mship.core.repo_state import audit_repos
            from mship.core.audit_gate import collect_known_worktree_paths
            config = container.config()
            shell = container.shell()
            try:
                known = collect_known_worktree_paths(state_mgr)
            except Exception:
                known = frozenset()
            report = audit_repos(
                config, shell, names=task.affected_repos,
                known_worktree_paths=known, local_only=True,
            )
            errors = [i for r in report.repos for i in r.issues if i.severity == "error"]
            drift_summary = {"has_errors": bool(errors), "error_count": len(errors)}
        except Exception:
            pass  # drift line simply omitted on failure

        # Last log
        last_log: dict | None = None
        try:
            entries = container.log_manager().read(task.slug, last=1)
            if entries:
                e = entries[-1]
                first_line = e.message.splitlines()[0] if e.message else ""
                last_log = {"message": first_line[:60], "timestamp": e.timestamp}
        except Exception:
            last_log = None

        if output.is_tty:
            output.print(f"[bold]Task:[/bold] {task.slug}")
            if task.finished_at is not None:
                output.print(
                    f"[yellow]⚠ Finished:[/yellow] {format_relative(task.finished_at)} — run `mship close` after merge"
                )
            phase_str = task.phase
            if task.phase_entered_at is not None:
                rel = format_relative(task.phase_entered_at)
                # Strip trailing " ago" for the inline phase line
                phase_str = f"{task.phase} (entered {rel})"
            if task.blocked_reason:
                phase_str = f"{phase_str}  [red]BLOCKED:[/red] {task.blocked_reason}"
            output.print(f"[bold]Phase:[/bold] {phase_str}")
            if task.blocked_at:
                output.print(f"[bold]Blocked since:[/bold] {task.blocked_at}")
            output.print(f"[bold]Branch:[/bold] {task.branch}")
            output.print(f"[bold]Repos:[/bold] {', '.join(task.affected_repos)}")
            if task.worktrees:
                output.print("[bold]Worktrees:[/bold]")
                for repo, path in task.worktrees.items():
                    output.print(f"  {repo}: {path}")
            if task.test_results:
                output.print("[bold]Tests:[/bold]")
                for repo, result in task.test_results.items():
                    status_str = (
                        "[green]pass[/green]" if result.status == "pass"
                        else "[red]fail[/red]"
                    )
                    output.print(f"  {repo}: {status_str}")
            if drift_summary["has_errors"]:
                output.print(
                    f"[bold]Drift:[/bold] [red]{drift_summary['error_count']} error(s)[/red] — run `mship audit`"
                )
            else:
                output.print("[bold]Drift:[/bold] [green]clean[/green]")
            if last_log is not None:
                ts_rel = format_relative(last_log["timestamp"])
                output.print(f"[bold]Last log:[/bold] \"{last_log['message']}\" ({ts_rel})")
        else:
            data = task.model_dump(mode="json")
            if task.blocked_reason:
                data["phase_display"] = f"{task.phase} (BLOCKED: {task.blocked_reason})"
            data["drift"] = drift_summary
            data["last_log"] = (
                {"message": last_log["message"], "timestamp": last_log["timestamp"].isoformat()}
                if last_log is not None else None
            )
            output.json(data)

    # Keep the existing `graph` command in this module untouched (if present below).
```

Preserve whatever `graph` command was already defined below the `status` command — only replace the `status` function body.

- [ ] **Step 4: Extend `cli/view/status.py`**

In `src/mship/cli/view/status.py`, update `StatusView.gather` to include the same lines:

```python
    def gather(self) -> str:
        from datetime import datetime, timezone
        from mship.util.duration import format_relative

        state = self._state_manager.load()
        if state.current_task is None:
            return "No active task"
        task = state.tasks[state.current_task]

        lines = [f"Task:   {task.slug}"]
        if task.finished_at is not None:
            lines.append(
                f"⚠ Finished: {format_relative(task.finished_at)} — run `mship close` after merge"
            )
        phase_line = task.phase
        if task.phase_entered_at is not None:
            phase_line = f"{task.phase} (entered {format_relative(task.phase_entered_at)})"
        if task.blocked_reason:
            phase_line += f"  (BLOCKED: {task.blocked_reason})"
        lines.append(f"Phase:  {phase_line}")
        lines.append(f"Branch: {task.branch}")
        lines.append(f"Repos:  {', '.join(task.affected_repos)}")
        if task.worktrees:
            lines.append("Worktrees:")
            for repo, path in task.worktrees.items():
                lines.append(f"  {repo}: {path}")
        if task.test_results:
            lines.append("Tests:")
            for repo, result in task.test_results.items():
                lines.append(f"  {repo}: {result.status}")
        return "\n".join(lines)
```

The TUI intentionally omits drift and last-log for now to avoid per-refresh subprocess overhead; the CLI variant runs them on-demand per invocation which is fine. If you want them in the TUI too, that's a follow-up.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/cli/test_status.py tests/cli/view/test_status_view.py -v`
Expected: all pass.

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/status.py src/mship/cli/view/status.py \
        tests/cli/test_status.py tests/cli/view/test_status_view.py
git commit -m "feat(status): enrich with drift, phase duration, last log, finished warning"
```

---

## Task 7: Documentation

**Files:**
- Modify: `skills/working-with-mothership/SKILL.md`
- Modify: `README.md`

- [ ] **Step 1: Update the skill document**

In `skills/working-with-mothership/SKILL.md`, replace every occurrence of `mship abort` with `mship close`, including in the command tables, examples, and prose. Add a short subsection under "Finishing work" that documents `--push-only`:

Find:
```
mship finish                            # creates coordinated PRs across repos in dependency order
mship finish --base main                # global override of PR base branch
mship finish --base-map cli=main,api=release/7  # per-repo PR base overrides
mship finish --handoff                  # write a CI handoff manifest instead
mship finish --force-audit              # bypass the drift audit gate (logged to task log)
mship abort --yes                       # remove worktrees and clean up state (best-effort cleanup on git failure)
```

Replace with:
```
mship finish                            # creates coordinated PRs across repos in dependency order
mship finish --base main                # global override of PR base branch
mship finish --base-map cli=main,api=release/7  # per-repo PR base overrides
mship finish --push-only                # push branches only; skip gh pr create (for non-PR flows)
mship finish --handoff                  # write a CI handoff manifest instead
mship finish --force-audit              # bypass the drift audit gate (logged to task log)
mship close [--yes] [--force] [--skip-pr-check]   # after PRs merged/closed: tear down worktrees, clear state
```

Add a new "What NOT to do" bullet:
```
- **Don't keep editing a worktree after `mship finish`** — the task is in an "awaiting resolution" state; `mship status` warns you, and `mship phase plan|dev|review` will refuse without --force. Run `mship close` after the PR is merged or cancelled, then `mship spawn` for the next task.
```

- [ ] **Step 2: Update the README**

In `README.md`, do the same rename (`mship abort` → `mship close`) and add `--push-only` to the CLI cheat sheet's `mship finish` line. Also update the 60-second example if it ends with `mship finish` to mention the close step briefly. Find the CLI cheat sheet block under `## CLI Reference` and update the finish-group lines to match the skill doc.

- [ ] **Step 3: Quick smoke check**

Run: `uv run mship close --help`
Expected: the help text shows `--yes`, `--force`, `--skip-pr-check`.

Run: `uv run mship finish --help`
Expected: `--push-only` appears.

- [ ] **Step 4: Commit**

```bash
git add skills/working-with-mothership/SKILL.md README.md
git commit -m "docs: rename abort→close, document --push-only, note finished-task guardrails"
```

---

## Self-Review

**Spec coverage:**
- `Task.finished_at`, `Task.phase_entered_at`: Task 1. ✓
- `format_relative` util: Task 1. ✓
- `PhaseManager` stamps `phase_entered_at`, guardrail + `--force`, `run` always allowed: Task 2. ✓
- CLI `mship phase --force` reuse: Task 2. ✓
- `PRManager.check_pr_state`: Task 3. ✓
- `audit_repos(local_only=True)`: Task 3. ✓
- `mship finish` stamps `finished_at`, nudge message: Task 4. ✓
- `mship finish --push-only` + incompatible-flag guard: Task 4. ✓
- `mship close` with PR-state resolution matrix: Task 5. ✓
- `--force`, `--yes`, `--skip-pr-check` flags on close: Task 5. ✓
- No-PR log entries (`cancelled before finish`, `no PRs (pushed via --push-only)`): Task 5. ✓
- `mship status` enrichment (drift / last log / phase duration / finished): Task 6. ✓
- `mship view status` enrichment: Task 6. ✓
- Docs: Task 7. ✓

**Placeholder scan:** none.

**Type consistency:**
- `check_pr_state(pr_url) -> str` returning `"merged"|"closed"|"open"|"unknown"` matches Task 5's router.
- `audit_repos(..., local_only: bool = False)` matches Task 6's status call.
- `FinishedTaskError` raised in Task 2, caught in Task 2's CLI update.
- `force_finished` kwarg name matches between `PhaseManager.transition` definition and the `cli/phase.py` call site.

**Known deferrals:**
- TUI view doesn't show drift/last-log (Task 6 note) — cosmetic follow-up.
- No `abort` deprecation shim (pre-1.0).
