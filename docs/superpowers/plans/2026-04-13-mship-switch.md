# `mship switch` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `mship switch [repo]` — atomic cross-repo context switch that records the active repo, snapshots each dependency's current HEAD SHA, and renders a structured handoff (deps changed since last switch, last log entry, drift summary, test status).

**Architecture:** Extend `Task` with `active_repo` and `last_switched_at_sha` fields. Add `core/switch.py` with pure `build_handoff(config, state, shell, repo)` returning a `Handoff` dataclass. Add `cli/switch.py` that mutates state at the top of the switch flow and hands off to the core layer for rendering. `mship status` surfaces the active repo.

**Tech Stack:** Python 3.12+, Typer, Pydantic, existing `ShellRunner` / `StateManager` / `LogManager` / `audit_repos(local_only=True)`.

**Spec:** `docs/superpowers/specs/2026-04-13-mship-switch-design.md`

---

## File Structure

**Create:**
- `src/mship/core/switch.py` — `DepChange`, `Handoff`, `build_handoff`.
- `src/mship/cli/switch.py` — `mship switch` command.
- `tests/core/test_switch.py`, `tests/cli/test_switch.py`.

**Modify:**
- `src/mship/core/state.py` — two new fields on `Task`.
- `src/mship/cli/__init__.py` — register sub-app.
- `src/mship/cli/status.py` — `Active repo:` line + JSON field.
- `src/mship/cli/view/status.py` — same in TUI.
- `skills/working-with-mothership/SKILL.md`, `README.md` — document the verb.

---

## Task 1: `Task` state fields

**Files:**
- Modify: `src/mship/core/state.py` (Task class, line 15)
- Test: `tests/core/test_state.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_state.py`:
```python
def test_task_accepts_active_repo_and_switched_sha_map(tmp_path):
    import yaml
    from datetime import datetime, timezone
    from pathlib import Path

    from mship.core.state import StateManager, Task, WorkspaceState

    sm = StateManager(tmp_path)
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="dev",
            created_at=datetime.now(timezone.utc),
            affected_repos=["a", "b"], branch="feat/t",
            active_repo="a",
            last_switched_at_sha={"a": {"b": "abc123"}},
        )},
    )
    sm.save(state)

    # Round-trip via the yaml file
    loaded = sm.load()
    task = loaded.tasks["t"]
    assert task.active_repo == "a"
    assert task.last_switched_at_sha == {"a": {"b": "abc123"}}


def test_task_defaults_for_switch_fields():
    from datetime import datetime, timezone
    from mship.core.state import Task

    task = Task(
        slug="t", description="d", phase="plan",
        created_at=datetime.now(timezone.utc),
        affected_repos=["a"], branch="feat/t",
    )
    assert task.active_repo is None
    assert task.last_switched_at_sha == {}
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/core/test_state.py -v -k "active_repo or switch_fields"`
Expected: FAIL — fields undefined.

- [ ] **Step 3: Add the fields**

In `src/mship/core/state.py`, extend the `Task` class (keep existing fields; add two at the end):
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
    active_repo: str | None = None
    last_switched_at_sha: dict[str, dict[str, str]] = {}
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_state.py -v`
Expected: PASS (new tests + existing state tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/state.py tests/core/test_state.py
git commit -m "feat(state): add active_repo and last_switched_at_sha to Task"
```

---

## Task 2: `build_handoff` core function

**Files:**
- Create: `src/mship/core/switch.py`
- Test: `tests/core/test_switch.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_switch.py`:
```python
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.config import ConfigLoader
from mship.core.log import LogManager
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.switch import build_handoff, DepChange, Handoff
from mship.util.shell import ShellRunner


def _sh(*args, cwd, env=None):
    e = {**os.environ,
         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if env is not None:
        e.update(env)
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=e)


def _head_sha(path: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True,
    )
    return r.stdout.strip()


@pytest.fixture
def switch_workspace(audit_workspace, tmp_path):
    """Extend audit_workspace so 'cli' depends on 'shared'. Both have worktrees for task 't'."""
    import yaml

    # Rename repos + add dependency edge: shared (library) ← cli (service, depends_on shared)
    cfg_path = audit_workspace / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "switch-test",
        "repos": {
            "shared": {"path": "./cli", "type": "library"},
            "cli":    {"path": "./api", "type": "service", "depends_on": ["shared"]},
        },
    }))
    # Move directories to match the rewritten config
    (audit_workspace / "cli").rename(audit_workspace / "_cli_tmp")
    (audit_workspace / "api").rename(audit_workspace / "_api_tmp")
    (audit_workspace / "_cli_tmp").rename(audit_workspace / "cli")   # shared -> ./cli
    (audit_workspace / "_api_tmp").rename(audit_workspace / "api")   # cli    -> ./api

    # Create a worktree per repo at .worktrees/feat/t
    shared_wt = audit_workspace / "shared-wt"
    cli_wt = audit_workspace / "cli-wt"
    _sh("git", "worktree", "add", str(shared_wt), "-b", "feat/t",
        cwd=audit_workspace / "cli")
    _sh("git", "worktree", "add", str(cli_wt), "-b", "feat/t",
        cwd=audit_workspace / "api")

    # Seed state
    state_dir = audit_workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    sm = StateManager(state_dir)
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="dev",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared", "cli"], branch="feat/t",
            worktrees={"shared": shared_wt, "cli": cli_wt},
        )},
    )
    sm.save(state)

    return audit_workspace, shared_wt, cli_wt, sm


def test_handoff_first_switch_uses_merge_base_fallback(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace

    # Commit something new in shared's worktree (goes into the feat/t branch)
    (shared_wt / "new.txt").write_text("hello\n")
    _sh("git", "add", "new.txt", cwd=shared_wt)
    _sh("git", "commit", "-qm", "add new.txt", cwd=shared_wt)

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    state = sm.load()

    handoff = build_handoff(cfg, state, shell, log_mgr, repo="cli")

    assert isinstance(handoff, Handoff)
    assert handoff.repo == "cli"
    assert handoff.worktree_path == cli_wt
    assert not handoff.worktree_missing
    # First switch — fallback anchor (merge-base) picks up the new shared commit
    dep_names = [d.repo for d in handoff.dep_changes]
    assert dep_names == ["shared"]
    shared_change = handoff.dep_changes[0]
    assert shared_change.commit_count >= 1
    assert shared_change.error is None
    assert "new.txt" in shared_change.files_changed


def test_handoff_subsequent_switch_uses_stored_sha(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace

    # First snapshot: shared's current SHA stored under last_switched_at_sha['cli']['shared']
    state = sm.load()
    state.tasks["t"].last_switched_at_sha = {"cli": {"shared": _head_sha(shared_wt)}}
    sm.save(state)

    # New commit in shared after the snapshot
    (shared_wt / "new.txt").write_text("hi\n")
    _sh("git", "add", "new.txt", cwd=shared_wt)
    _sh("git", "commit", "-qm", "post-snapshot", cwd=shared_wt)

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")

    shared_change = handoff.dep_changes[0]
    assert shared_change.commit_count == 1
    assert "post-snapshot" in shared_change.commits[0]


def test_handoff_clean_deps_omitted(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace
    # No new commits in shared; the first-switch fallback anchor is merge-base == HEAD
    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert handoff.dep_changes == ()


def test_handoff_missing_dep_worktree(switch_workspace):
    import shutil
    workspace, shared_wt, cli_wt, sm = switch_workspace
    shutil.rmtree(shared_wt)
    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert len(handoff.dep_changes) == 1
    assert handoff.dep_changes[0].repo == "shared"
    assert handoff.dep_changes[0].error is not None
    assert "worktree" in handoff.dep_changes[0].error.lower()


def test_handoff_missing_switched_to_worktree(switch_workspace):
    import shutil
    workspace, shared_wt, cli_wt, sm = switch_workspace
    shutil.rmtree(cli_wt)
    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert handoff.worktree_missing is True


def test_handoff_includes_finished_at(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace
    state = sm.load()
    state.tasks["t"].finished_at = datetime.now(timezone.utc)
    sm.save(state)
    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert handoff.finished_at is not None


def test_handoff_last_log_entry(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    log_mgr.append("t", "wired Label into middleware")

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert handoff.last_log_in_repo is not None
    assert "Label" in handoff.last_log_in_repo.message


def test_handoff_drift_count_nonzero_when_dirty(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace
    (cli_wt / "dirty.txt").write_text("x\n")

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert handoff.drift_error_count >= 1
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_switch.py -v`
Expected: FAIL — `mship.core.switch` missing.

- [ ] **Step 3: Implement `switch.py`**

Create `src/mship/core/switch.py`:
```python
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


def _fallback_anchor(shell: ShellRunner, dep_worktree: Path, task_branch: str,
                      dep_config, cli_fallback: str | None) -> str | None:
    """First-switch anchor: merge-base of task branch with the dep's base branch.

    Order: configured base_branch, origin/HEAD symbolic ref, None.
    """
    base = dep_config.base_branch if dep_config.base_branch is not None else None
    candidates: list[str] = []
    if base is not None:
        candidates.append(base)
        candidates.append(f"origin/{base}")
    candidates.append("origin/HEAD")
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
    rc, out, err = _run(shell, f"git log --format=%h %s {shlex.quote(spec)}", dep_worktree)
    if rc != 0:
        return DepChange(
            repo=dep_name, commit_count=0, commits=(), files_changed=(),
            additions=0, deletions=0, error=(err.strip().splitlines()[-1] if err.strip() else "git log failed"),
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

    # Drift (local-only, scoped)
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
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/core/test_switch.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/switch.py tests/core/test_switch.py
git commit -m "feat: add build_handoff for cross-repo context switch"
```

---

## Task 3: `mship switch` CLI command

**Files:**
- Create: `src/mship/cli/switch.py`
- Modify: `src/mship/cli/__init__.py`
- Test: `tests/cli/test_switch.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_switch.py`:
```python
import json
import os
import subprocess
from datetime import datetime, timezone

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


runner = CliRunner()


def _sh(*args, cwd):
    e = {**os.environ,
         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=e)


def _override(path):
    container.config_path.override(path / "mothership.yaml")
    container.state_dir.override(path / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def _seed_switchable(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace
    return workspace


def test_switch_records_active_repo_and_exits_zero(switch_workspace):
    workspace = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "cli"])
        assert result.exit_code == 0, result.output
        sm = StateManager(workspace / ".mothership")
        state = sm.load()
        assert state.tasks["t"].active_repo == "cli"
        # Shared's SHA snapshotted under last_switched_at_sha["cli"]
        assert "cli" in state.tasks["t"].last_switched_at_sha
        assert "shared" in state.tasks["t"].last_switched_at_sha["cli"]
    finally:
        _reset()


def test_switch_bogus_repo_errors(switch_workspace):
    workspace = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "nope"])
        assert result.exit_code != 0
        assert "nope" in result.output.lower()
    finally:
        _reset()


def test_switch_no_active_task_errors(tmp_path):
    # Empty workspace — no task seeded
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text("workspace: empty\nrepos: {}\n")
    _override(tmp_path)
    try:
        result = runner.invoke(app, ["switch", "whatever"])
        assert result.exit_code != 0
        assert "no active task" in result.output.lower() or "no such command" not in result.output.lower()
    finally:
        _reset()


def test_switch_bare_rerenders_active(switch_workspace):
    workspace = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "cli"])
        assert result.exit_code == 0, result.output

        result2 = runner.invoke(app, ["switch"])
        assert result2.exit_code == 0, result2.output
        # Either "Switched to" (non-TTY JSON won't contain this; be permissive)
        # or the JSON payload with repo=cli.
        try:
            payload = json.loads(result2.output)
            assert payload["repo"] == "cli"
        except json.JSONDecodeError:
            assert "cli" in result2.output
    finally:
        _reset()


def test_switch_bare_no_active_repo_errors(switch_workspace):
    workspace = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch"])
        assert result.exit_code != 0
        assert "no active repo" in result.output.lower() or "switch <repo>" in result.output
    finally:
        _reset()


def test_switch_json_shape(switch_workspace):
    workspace = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "cli"])
        assert result.exit_code == 0, result.output
        # CliRunner is non-TTY → JSON output
        payload = json.loads(result.output)
        assert payload["repo"] == "cli"
        assert payload["task_slug"] == "t"
        assert "dep_changes" in payload
        assert "drift_error_count" in payload
    finally:
        _reset()
```

Make the `switch_workspace` fixture reusable by moving it from `tests/core/test_switch.py` into `tests/conftest.py`. Cut the fixture (including the helper `_sh` calls and imports it needs) out of `tests/core/test_switch.py` and paste it into `tests/conftest.py`. Leave `_head_sha` as a local helper in `tests/core/test_switch.py` since it's only used there.

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_switch.py -v`
Expected: FAIL — `switch` command not registered.

- [ ] **Step 3: Implement the CLI**

Create `src/mship/cli/switch.py`:
```python
from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def switch(
        repo: Optional[str] = typer.Argument(None, help="Repo to switch to. Omit to re-render current."),
    ):
        """Switch active repo within the current task; emit an orientation handoff."""
        import subprocess
        import shlex
        from datetime import datetime, timezone
        from pathlib import Path

        from mship.core.switch import build_handoff
        from mship.util.duration import format_relative

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()
        config = container.config()
        shell = container.shell()
        log_mgr = container.log_manager()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]

        if repo is None:
            if task.active_repo is None:
                output.error("No active repo. Run `mship switch <repo>` first.")
                raise typer.Exit(code=1)
            target = task.active_repo
            is_switch = False
        else:
            if repo not in task.affected_repos:
                valid = ", ".join(task.affected_repos)
                output.error(f"Unknown repo '{repo}'. Valid: {valid}.")
                raise typer.Exit(code=1)
            target = repo
            is_switch = True

            # Snapshot every dep's current HEAD SHA before rendering.
            snapshot: dict[str, str] = {}
            repo_cfg = config.repos[target]
            for dep in repo_cfg.depends_on:
                dep_name = dep.repo
                dep_wt = task.worktrees.get(dep_name)
                if dep_wt is None or not Path(dep_wt).exists():
                    continue
                result = shell.run("git rev-parse HEAD", cwd=Path(dep_wt))
                if result.returncode == 0 and result.stdout.strip():
                    snapshot[dep_name] = result.stdout.strip()

            task.active_repo = target
            task.last_switched_at_sha[target] = snapshot
            state_mgr.save(state)

        handoff = build_handoff(config, state_mgr.load(), shell, log_mgr, repo=target)

        if not output.is_tty:
            output.json(handoff.to_json())
            return

        # TTY rendering
        verb = "Switched to" if is_switch else "Currently at"
        lines: list[str] = []
        if handoff.worktree_missing:
            lines.append(
                f"[red]⚠ worktree missing:[/red] {handoff.worktree_path} "
                f"(run `mship prune` or `mship close`)"
            )
        if handoff.finished_at is not None:
            lines.append(
                f"[yellow]⚠ task finished {format_relative(handoff.finished_at)}[/yellow] "
                f"— run `mship close` after merge"
            )
        lines.append(
            f"[bold]{verb}:[/bold] {handoff.repo} (task: {handoff.task_slug}, phase: {handoff.phase})"
        )
        lines.append(f"[bold]Branch:[/bold]   {handoff.branch}")
        lines.append(f"[bold]Worktree:[/bold] {handoff.worktree_path}")
        lines.append("")

        if handoff.dep_changes:
            lines.append("[bold]Dependencies changed since your last switch here:[/bold]")
            for d in handoff.dep_changes:
                if d.error is not None:
                    lines.append(f"  [red]{d.repo}: {d.error}[/red]")
                    continue
                lines.append(f"  [green]{d.repo}[/green] ({d.commit_count} commits):")
                for c in d.commits:
                    lines.append(f"    {c}")
                files_str = ", ".join(d.files_changed) if d.files_changed else "(no files)"
                lines.append(
                    f"    files:   {files_str}  (+{d.additions} -{d.deletions})"
                )
        else:
            lines.append("[dim]Dependencies: no changes since last switch.[/dim]")
        lines.append("")

        if handoff.last_log_in_repo is not None:
            first_line = handoff.last_log_in_repo.message.splitlines()[0]
            rel = format_relative(handoff.last_log_in_repo.timestamp)
            lines.append(f"[bold]Your last log:[/bold] \"{first_line[:80]}\" ({rel})")

        if handoff.drift_error_count > 0:
            lines.append(f"[bold]Drift:[/bold] [red]{handoff.drift_error_count} error(s)[/red] — run `mship audit`")
        else:
            lines.append("[bold]Drift:[/bold] [green]clean[/green]")

        if handoff.test_status is None:
            lines.append("[bold]Tests:[/bold] not run yet")
        else:
            color = "green" if handoff.test_status == "pass" else "red"
            lines.append(f"[bold]Tests:[/bold] [{color}]{handoff.test_status}[/{color}]")

        for line in lines:
            output.print(line)
```

In `src/mship/cli/__init__.py`, register the new module alongside the others:

```python
from mship.cli import switch as _switch_mod
...
_switch_mod.register(app, get_container)
```

(Insert both lines in the same pattern as existing modules — import grouped with the other `_*_mod` imports, and the register call grouped with the other register calls.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cli/test_switch.py -v`
Expected: PASS.

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/switch.py src/mship/cli/__init__.py tests/cli/test_switch.py tests/conftest.py
git commit -m "feat: add mship switch command with handoff rendering"
```

---

## Task 4: `mship status` + `mship view status` show active_repo

**Files:**
- Modify: `src/mship/cli/status.py`
- Modify: `src/mship/cli/view/status.py`
- Test: `tests/cli/test_status.py`
- Test: `tests/cli/view/test_status_view.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_status.py`:
```python
def test_status_shows_active_repo(workspace_with_git):
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        phase_entered_at=datetime.now(timezone.utc),
        affected_repos=["shared", "auth-service"], branch="feat/t",
        active_repo="shared",
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        import json as _j
        try:
            payload = _j.loads(result.output)
            assert payload["active_repo"] == "shared"
        except _j.JSONDecodeError:
            assert "Active repo" in result.output
            assert "shared" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
```

Append to `tests/cli/view/test_status_view.py`:
```python
@pytest.mark.asyncio
async def test_status_view_shows_active_repo():
    from datetime import datetime, timezone

    class _Task:
        slug = "t"
        phase = "dev"
        phase_entered_at = datetime.now(timezone.utc)
        blocked_reason = None
        blocked_at = None
        branch = "feat/t"
        affected_repos = ["a", "b"]
        worktrees = {}
        test_results = {}
        pr_urls = {}
        finished_at = None
        active_repo = "a"

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
        assert "Active repo" in text
        assert "a" in text
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_status.py tests/cli/view/test_status_view.py -v -k "active_repo"`
Expected: FAIL.

- [ ] **Step 3: Update `cli/status.py`**

In the TTY branch of `status`, after the existing `Task:` line and before the `Phase:` line, insert:
```python
            if task.active_repo is not None:
                output.print(f"[bold]Active repo:[/bold] {task.active_repo}")
```

In the non-TTY (JSON) path, add `"active_repo": task.active_repo` to the serialized dict (the `data` local is already `task.model_dump(mode="json")`, which includes this field now that it's on the model — but explicitly add `data["active_repo"] = task.active_repo` after the `data = task.model_dump(...)` line to make the contract explicit).

- [ ] **Step 4: Update `cli/view/status.py`**

In `StatusView.gather`, after appending the `Task:` line and before the `Phase:` line, insert:
```python
        if getattr(task, "active_repo", None) is not None:
            lines.append(f"Active: {task.active_repo}")
```

Use the label "Active" (not "Active repo") so the TUI stays compact; tests assert on "Active repo" for the CLI path and "Active" for the TUI path. Update the test accordingly if you match differently.

Actually — keep both labels consistent. Change the TUI label to `Active repo` to match the CLI output (the TUI has enough width). The test already asserts `"Active repo" in text` for the TUI variant.

Revised TUI insertion:
```python
        if getattr(task, "active_repo", None) is not None:
            lines.append(f"Active repo: {task.active_repo}")
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/cli/test_status.py tests/cli/view/test_status_view.py -v`
Expected: PASS.

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/status.py src/mship/cli/view/status.py \
        tests/cli/test_status.py tests/cli/view/test_status_view.py
git commit -m "feat(status): show active_repo in mship status and mship view status"
```

---

## Task 5: Documentation — skill + README

**Files:**
- Modify: `skills/working-with-mothership/SKILL.md`
- Modify: `README.md`

- [ ] **Step 1: Update the skill**

In `skills/working-with-mothership/SKILL.md`, under the "During work" section (where existing commands like `mship phase dev`, `mship test`, `mship log` are listed), add:
```
mship switch <repo>                   # set active repo + emit handoff (deps changed, last log, drift, tests)
mship switch                           # re-render handoff for the currently active repo
```

Also update the session-start protocol section so it reads:
```
Every session, before doing anything else:

```bash
mship status    # current task, phase, active repo, worktrees, drift, last log
mship log       # full narrative of what was happening last session
mship switch <repo>   # if you're about to work in a specific repo, call this first
                       # (snapshots dep SHAs + shows what changed since you were last here)
```
```

Add a new "What NOT to do" bullet:
```
- **Don't `cd` between worktrees without `mship switch`** — you'll miss cross-repo changes and lose the "since your last switch" anchor. Always call `mship switch <repo>` before starting work in a different repo.
```

- [ ] **Step 2: Update the README**

In `README.md`, under the CLI cheat sheet, add to the "Phase management" block (or create a new "Context" block if cleaner):
```
mship switch <repo>                   # cross-repo context switch: handoff + record active repo
mship switch                           # re-render handoff for the currently active repo
```

Also add a short paragraph under the state-safety narrative (near the "How mship prevents them" table), something like:
```
**Cross-repo context switches.** When the agent moves between repos within a task, `mship switch <repo>` records the checkpoint and emits a structured handoff: what changed in dependency repos since the agent was last here, what it logged in this repo, whether the worktree is clean. The agent re-injects the handoff into its context and continues work grounded in current state — no re-reading every file, no stale mental models, no running tests against the wrong version of a dependency.
```

- [ ] **Step 3: Smoke check**

Run: `uv run mship switch --help`
Expected: shows the command; argument is optional.

- [ ] **Step 4: Commit**

```bash
git add skills/working-with-mothership/SKILL.md README.md
git commit -m "docs: document mship switch in skill and README"
```

---

## Self-Review

**Spec coverage:**
- `Task.active_repo`, `Task.last_switched_at_sha` — Task 1.
- `DepChange`, `Handoff`, `build_handoff` — Task 2.
- Merge-base first-switch fallback — Task 2 (`_fallback_anchor`).
- Missing dep worktree → error entry — Task 2.
- Missing switched-to worktree → `worktree_missing=True` — Task 2.
- Drift (local-only, scoped), last log, test status — Task 2.
- Finished-at surfacing — Task 2.
- `mship switch <repo>` snapshotting + state save — Task 3.
- `mship switch` bare re-rendering active — Task 3.
- Unknown repo / no active task / no active repo errors — Task 3.
- JSON output — Task 3.
- TTY rendering with warnings prepended — Task 3.
- `mship status` and `mship view status` `active_repo` line — Task 4.
- Skill + README updates — Task 5.

**Placeholder scan:** none.

**Type consistency:**
- `build_handoff(config, state, shell, log_manager, repo)` signature matches between Task 2 definition and Task 3 caller.
- `Handoff.to_json()` keys match between Task 2 impl and Task 3 JSON test assertions.
- `last_switched_at_sha: dict[str, dict[str, str]]` shape matches between Task 1 state def, Task 3 snapshot writer, Task 2 reader.

**Known deferrals (post-v1):**
- Structured log tags (will improve `last_log_in_repo` accuracy).
- `--shell` eval integration for `cd`.
- Warnings on `mship <verb>` commands used from the wrong worktree.
