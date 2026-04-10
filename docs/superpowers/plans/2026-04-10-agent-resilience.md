# Agent Resilience Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add blocked state tracking, per-task context logs, CI/CD handoff manifests, and worktree garbage collection to mothership.

**Architecture:** Four additive features that extend existing core/CLI layers. LogManager and PruneManager are new services added to the DI container. Blocked state adds fields to the existing Task model. Handoff adds a `--handoff` flag to the existing `finish` command.

**Tech Stack:** Python 3.14, Pydantic v2, Typer, PyYAML (existing stack — no new dependencies)

---

## File Map

### Core layer (new/modified)
- `src/mship/core/state.py` — modify: add `blocked_reason`, `blocked_at` to Task model
- `src/mship/core/log.py` — create: LogManager for per-task append-only markdown logs
- `src/mship/core/handoff.py` — create: HandoffManifest Pydantic model and generation
- `src/mship/core/prune.py` — create: PruneManager for orphan detection and cleanup
- `src/mship/core/phase.py` — modify: clear blocked state on phase transition

### CLI layer (new/modified)
- `src/mship/cli/block.py` — create: `mship block` and `mship unblock` commands
- `src/mship/cli/log.py` — create: `mship log` command
- `src/mship/cli/prune.py` — create: `mship prune` command
- `src/mship/cli/worktree.py` — modify: add `--handoff` flag to `finish`
- `src/mship/cli/status.py` — modify: show blocked overlay
- `src/mship/cli/__init__.py` — modify: register new command modules

### DI container
- `src/mship/container.py` — modify: add LogManager, PruneManager providers

### Tests
- `tests/core/test_state.py` — modify: test new blocked fields
- `tests/core/test_log.py` — create: LogManager tests
- `tests/core/test_handoff.py` — create: HandoffManifest tests
- `tests/core/test_prune.py` — create: PruneManager tests
- `tests/core/test_phase.py` — modify: test blocked clearing on transition
- `tests/cli/test_block.py` — create: block/unblock CLI tests
- `tests/cli/test_log.py` — create: log CLI tests
- `tests/cli/test_prune.py` — create: prune CLI tests
- `tests/cli/test_worktree.py` — modify: test `--handoff` flag

---

### Task 1: Add Blocked Fields to Task Model

**Files:**
- Modify: `src/mship/core/state.py:15-23`
- Modify: `tests/core/test_state.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_state.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

from mship.core.state import StateManager, Task, WorkspaceState


def test_task_blocked_fields_default_none(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="test",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/test",
    )
    assert task.blocked_reason is None
    assert task.blocked_at is None


def test_task_blocked_roundtrip(state_dir: Path):
    mgr = StateManager(state_dir)
    now = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    task = Task(
        slug="test",
        description="Test",
        phase="dev",
        created_at=now,
        affected_repos=["shared"],
        branch="feat/test",
        blocked_reason="waiting on API key",
        blocked_at=now,
    )
    state = WorkspaceState(current_task="test", tasks={"test": task})
    mgr.save(state)
    loaded = mgr.load()
    assert loaded.tasks["test"].blocked_reason == "waiting on API key"
    assert loaded.tasks["test"].blocked_at == now


def test_task_blocked_cleared(state_dir: Path):
    mgr = StateManager(state_dir)
    now = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    task = Task(
        slug="test",
        description="Test",
        phase="dev",
        created_at=now,
        affected_repos=["shared"],
        branch="feat/test",
        blocked_reason="waiting",
        blocked_at=now,
    )
    state = WorkspaceState(current_task="test", tasks={"test": task})
    mgr.save(state)
    loaded = mgr.load()
    loaded.tasks["test"].blocked_reason = None
    loaded.tasks["test"].blocked_at = None
    mgr.save(loaded)
    reloaded = mgr.load()
    assert reloaded.tasks["test"].blocked_reason is None
    assert reloaded.tasks["test"].blocked_at is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_state.py -v -k "blocked"`
Expected: FAIL — `blocked_reason` is not a valid field

- [ ] **Step 3: Add blocked fields to Task model**

Modify `src/mship/core/state.py`, add two fields to the `Task` class after `test_results`:

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_state.py -v -k "blocked"`
Expected: All 3 new tests PASS.

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS (blocked fields are optional so existing tests are unaffected).

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/state.py tests/core/test_state.py
git commit -m "feat: add blocked_reason and blocked_at fields to Task model"
```

---

### Task 2: LogManager Core

**Files:**
- Create: `src/mship/core/log.py`
- Create: `tests/core/test_log.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_log.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.log import LogManager, LogEntry


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".mothership" / "logs"
    d.mkdir(parents=True)
    return d


def test_create_log(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    assert (logs_dir / "add-labels.md").exists()
    content = (logs_dir / "add-labels.md").read_text()
    assert "# Task Log: add-labels" in content


def test_append_entry(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    mgr.append("add-labels", "Refactored auth controller")
    content = (logs_dir / "add-labels.md").read_text()
    assert "Refactored auth controller" in content


def test_append_multiple(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    mgr.append("add-labels", "First entry")
    mgr.append("add-labels", "Second entry")
    entries = mgr.read("add-labels")
    messages = [e.message for e in entries]
    assert "First entry" in messages
    assert "Second entry" in messages


def test_read_empty_log(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    entries = mgr.read("add-labels")
    assert entries == []


def test_read_last_n(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    mgr.append("add-labels", "First")
    mgr.append("add-labels", "Second")
    mgr.append("add-labels", "Third")
    entries = mgr.read("add-labels", last=2)
    assert len(entries) == 2
    assert entries[0].message == "Second"
    assert entries[1].message == "Third"


def test_read_nonexistent_log(logs_dir: Path):
    mgr = LogManager(logs_dir)
    entries = mgr.read("nonexistent")
    assert entries == []


def test_log_entry_has_timestamp(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    mgr.append("add-labels", "Test entry")
    entries = mgr.read("add-labels")
    assert len(entries) == 1
    assert isinstance(entries[0].timestamp, datetime)
    assert entries[0].message == "Test entry"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_log.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.log'`

- [ ] **Step 3: Write the implementation**

`src/mship/core/log.py`:
```python
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class LogEntry:
    timestamp: datetime
    message: str


class LogManager:
    """Per-task append-only markdown logs for agent context recovery."""

    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir

    def _log_path(self, task_slug: str) -> Path:
        return self._logs_dir / f"{task_slug}.md"

    def create(self, task_slug: str) -> None:
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_path(task_slug)
        path.write_text(f"# Task Log: {task_slug}\n")

    def append(self, task_slug: str, message: str) -> None:
        path = self._log_path(task_slug)
        if not path.exists():
            self.create(task_slug)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "a") as f:
            f.write(f"\n## {timestamp}\n{message}\n")

    def read(self, task_slug: str, last: int | None = None) -> list[LogEntry]:
        path = self._log_path(task_slug)
        if not path.exists():
            return []
        content = path.read_text()
        entries = self._parse(content)
        if last is not None:
            entries = entries[-last:]
        return entries

    def _parse(self, content: str) -> list[LogEntry]:
        entries: list[LogEntry] = []
        pattern = re.compile(
            r"^## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\n(.*?)(?=\n## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        for match in pattern.finditer(content):
            timestamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            message = match.group(2).strip()
            if message:
                entries.append(LogEntry(timestamp=timestamp, message=message))
        return entries
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_log.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/log.py tests/core/test_log.py
git commit -m "feat: add LogManager for per-task context recovery logs"
```

---

### Task 3: Wire LogManager into DI Container and Auto-Log Lifecycle Events

**Files:**
- Modify: `src/mship/container.py`
- Modify: `src/mship/core/worktree.py`
- Modify: `src/mship/core/phase.py`

- [ ] **Step 1: Add LogManager to container**

Modify `src/mship/container.py` — add import and provider:

```python
from mship.core.log import LogManager

# Inside Container class, after phase_manager:
    log_manager = providers.Singleton(
        LogManager,
        logs_dir=providers.Factory(
            lambda state_dir: state_dir / "logs",
            state_dir,
        ),
    )
```

- [ ] **Step 2: Add LogManager dependency to WorktreeManager**

Modify `src/mship/core/worktree.py` — add `log` parameter to `__init__` and auto-log on spawn:

```python
from mship.core.log import LogManager

class WorktreeManager:
    def __init__(
        self,
        config: WorkspaceConfig,
        graph: DependencyGraph,
        state_manager: StateManager,
        git: GitRunner,
        shell: ShellRunner,
        log: LogManager,
    ) -> None:
        self._config = config
        self._graph = graph
        self._state_manager = state_manager
        self._git = git
        self._shell = shell
        self._log = log
```

In the `spawn` method, after saving state and before `return task`:

```python
        self._log.create(slug)
        self._log.append(
            slug,
            f"Task spawned. Repos: {', '.join(ordered)}. Branch: {branch}",
        )
```

- [ ] **Step 3: Add LogManager dependency to PhaseManager**

Modify `src/mship/core/phase.py` — add `log` parameter and auto-log transitions:

```python
from mship.core.log import LogManager

class PhaseManager:
    def __init__(self, state_manager: StateManager, log: LogManager) -> None:
        self._state_manager = state_manager
        self._log = log

    def transition(self, task_slug: str, target: Phase) -> PhaseTransition:
        state = self._state_manager.load()
        task = state.tasks[task_slug]
        old_phase = task.phase
        warnings = self._check_gates(task_slug, task.phase, target)

        # Clear blocked state on phase transition
        if task.blocked_reason is not None:
            self._log.append(
                task_slug,
                f"Unblocked (phase transition to {target})",
            )
            task.blocked_reason = None
            task.blocked_at = None

        task.phase = target
        self._state_manager.save(state)

        self._log.append(task_slug, f"Phase transition: {old_phase} → {target}")

        return PhaseTransition(new_phase=target, warnings=warnings)
```

- [ ] **Step 4: Update container wiring**

Modify `src/mship/container.py` — update `worktree_manager` and `phase_manager` to include `log`:

```python
    worktree_manager = providers.Factory(
        WorktreeManager,
        config=config,
        graph=graph,
        state_manager=state_manager,
        git=git,
        shell=shell,
        log=log_manager,
    )

    phase_manager = providers.Factory(
        PhaseManager,
        state_manager=state_manager,
        log=log_manager,
    )
```

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS. Existing tests don't assert on log files so they'll pass even though lifecycle events are now logged.

- [ ] **Step 6: Commit**

```bash
git add src/mship/container.py src/mship/core/worktree.py src/mship/core/phase.py
git commit -m "feat: wire LogManager into DI container, auto-log spawn and phase transitions"
```

---

### Task 4: Block/Unblock CLI Commands

**Files:**
- Create: `src/mship/cli/block.py`
- Create: `tests/cli/test_block.py`
- Modify: `src/mship/cli/__init__.py`
- Modify: `src/mship/cli/status.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_block.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


@pytest.fixture
def configured_app_with_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(current_task="add-labels", tasks={"add-labels": task}))
    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_block(configured_app_with_task: Path):
    result = runner.invoke(app, ["block", "waiting on API key"])
    assert result.exit_code == 0
    mgr = StateManager(configured_app_with_task / ".mothership")
    state = mgr.load()
    assert state.tasks["add-labels"].blocked_reason == "waiting on API key"
    assert state.tasks["add-labels"].blocked_at is not None


def test_unblock(configured_app_with_task: Path):
    runner.invoke(app, ["block", "waiting"])
    result = runner.invoke(app, ["unblock"])
    assert result.exit_code == 0
    mgr = StateManager(configured_app_with_task / ".mothership")
    state = mgr.load()
    assert state.tasks["add-labels"].blocked_reason is None
    assert state.tasks["add-labels"].blocked_at is None


def test_unblock_when_not_blocked(configured_app_with_task: Path):
    result = runner.invoke(app, ["unblock"])
    assert result.exit_code != 0 or "not blocked" in result.output.lower()


def test_block_no_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["block", "reason"])
    assert result.exit_code != 0 or "No active task" in result.output
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_status_shows_blocked(configured_app_with_task: Path):
    runner.invoke(app, ["block", "waiting on API key"])
    result = runner.invoke(app, ["status"])
    assert "BLOCKED" in result.output
    assert "waiting on API key" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_block.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write `src/mship/cli/block.py`**

```python
from datetime import datetime, timezone

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def block(reason: str):
        """Mark the current task as blocked."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        task.blocked_reason = reason
        task.blocked_at = datetime.now(timezone.utc)
        state_mgr.save(state)

        log_mgr = container.log_manager()
        log_mgr.append(state.current_task, f"Blocked: {reason}")

        if output.is_tty:
            output.success(f"Task blocked: {reason}")
        else:
            output.json({"task": state.current_task, "blocked_reason": reason})

    @app.command()
    def unblock():
        """Clear the blocked state on the current task."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        if task.blocked_reason is None:
            output.error("Task is not blocked")
            raise typer.Exit(code=1)

        task.blocked_reason = None
        task.blocked_at = None
        state_mgr.save(state)

        log_mgr = container.log_manager()
        log_mgr.append(state.current_task, "Unblocked")

        if output.is_tty:
            output.success("Task unblocked")
        else:
            output.json({"task": state.current_task, "blocked_reason": None})
```

- [ ] **Step 4: Update status display for blocked overlay**

Modify `src/mship/cli/status.py` — after the `Phase` line (line 24), add blocked display:

```python
            phase_str = task.phase
            if task.blocked_reason:
                phase_str = f"{task.phase} (BLOCKED: {task.blocked_reason})"
            output.print(f"[bold]Phase:[/bold] {phase_str}")
            if task.blocked_at:
                output.print(f"[bold]Blocked since:[/bold] {task.blocked_at}")
```

Replace the existing line 24 (`output.print(f"[bold]Phase:[/bold] {task.phase}")`) with the above block.

- [ ] **Step 5: Register in `cli/__init__.py`**

Add to `src/mship/cli/__init__.py`:

```python
from mship.cli import block as _block_mod

_block_mod.register(app, get_container)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_block.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 7: Run full test suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/mship/cli/block.py src/mship/cli/status.py src/mship/cli/__init__.py tests/cli/test_block.py
git commit -m "feat: add mship block/unblock commands with status overlay"
```

---

### Task 5: Log CLI Command

**Files:**
- Create: `src/mship/cli/log.py`
- Create: `tests/cli/test_log.py`
- Modify: `src/mship/cli/__init__.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_log.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.log import LogManager

runner = CliRunner()


@pytest.fixture
def configured_app_with_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(current_task="add-labels", tasks={"add-labels": task}))

    # Create the log file
    log_mgr = LogManager(state_dir / "logs")
    log_mgr.create("add-labels")

    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_log_append(configured_app_with_task: Path):
    result = runner.invoke(app, ["log", "Refactored auth controller"])
    assert result.exit_code == 0


def test_log_read(configured_app_with_task: Path):
    runner.invoke(app, ["log", "First entry"])
    runner.invoke(app, ["log", "Second entry"])
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0
    assert "First entry" in result.output
    assert "Second entry" in result.output


def test_log_last_n(configured_app_with_task: Path):
    runner.invoke(app, ["log", "First"])
    runner.invoke(app, ["log", "Second"])
    runner.invoke(app, ["log", "Third"])
    result = runner.invoke(app, ["log", "--last", "1"])
    assert result.exit_code == 0
    assert "Third" in result.output
    assert "First" not in result.output


def test_log_no_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["log"])
    assert result.exit_code != 0 or "No active task" in result.output
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_log.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write the implementation**

`src/mship/cli/log.py`:
```python
from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(name="log")
    def log_cmd(
        message: Optional[str] = typer.Argument(None, help="Message to append to task log"),
        last: Optional[int] = typer.Option(None, "--last", help="Show only last N entries"),
    ):
        """Append to or read the current task's log."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task")
            raise typer.Exit(code=1)

        log_mgr = container.log_manager()

        if message is not None:
            log_mgr.append(state.current_task, message)
            if output.is_tty:
                output.success("Logged")
            else:
                output.json({"task": state.current_task, "logged": message})
        else:
            entries = log_mgr.read(state.current_task, last=last)
            if not entries:
                output.print("No log entries")
                return
            if output.is_tty:
                for entry in entries:
                    output.print(f"[dim]{entry.timestamp.strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
                    output.print(f"  {entry.message}")
            else:
                output.json({
                    "task": state.current_task,
                    "entries": [
                        {"timestamp": e.timestamp.isoformat(), "message": e.message}
                        for e in entries
                    ],
                })
```

- [ ] **Step 4: Register in `cli/__init__.py`**

Add to `src/mship/cli/__init__.py`:

```python
from mship.cli import log as _log_mod

_log_mod.register(app, get_container)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_log.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/log.py src/mship/cli/__init__.py tests/cli/test_log.py
git commit -m "feat: add mship log command for context recovery"
```

---

### Task 6: Handoff Manifest

**Files:**
- Create: `src/mship/core/handoff.py`
- Create: `tests/core/test_handoff.py`
- Modify: `src/mship/cli/worktree.py`
- Modify: `tests/cli/test_worktree.py`

- [ ] **Step 1: Write the core tests**

`tests/core/test_handoff.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from mship.core.handoff import HandoffManifest, MergeOrderEntry, generate_handoff


def test_handoff_manifest_model():
    entry = MergeOrderEntry(
        order=1,
        repo="shared",
        path=Path("./shared"),
        branch="feat/test",
        depends_on=[],
        pr=None,
    )
    manifest = HandoffManifest(
        task="add-labels",
        branch="feat/add-labels",
        generated_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        merge_order=[entry],
    )
    assert manifest.task == "add-labels"
    assert len(manifest.merge_order) == 1


def test_generate_handoff(tmp_path: Path):
    handoffs_dir = tmp_path / ".mothership" / "handoffs"
    handoffs_dir.mkdir(parents=True)

    ordered_repos = ["shared", "auth-service"]
    repo_paths = {"shared": Path("./shared"), "auth-service": Path("./auth-service")}
    repo_deps = {"shared": [], "auth-service": ["shared"]}

    path = generate_handoff(
        handoffs_dir=handoffs_dir,
        task_slug="add-labels",
        branch="feat/add-labels",
        ordered_repos=ordered_repos,
        repo_paths=repo_paths,
        repo_deps=repo_deps,
    )

    assert path.exists()
    with open(path) as f:
        data = yaml.safe_load(f)
    assert data["task"] == "add-labels"
    assert len(data["merge_order"]) == 2
    assert data["merge_order"][0]["repo"] == "shared"
    assert data["merge_order"][0]["order"] == 1
    assert data["merge_order"][1]["depends_on"] == ["shared"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_handoff.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

`src/mship/core/handoff.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel


class MergeOrderEntry(BaseModel):
    order: int
    repo: str
    path: Path
    branch: str
    depends_on: list[str]
    pr: str | None = None


class HandoffManifest(BaseModel):
    task: str
    branch: str
    generated_at: datetime
    merge_order: list[MergeOrderEntry]


def generate_handoff(
    handoffs_dir: Path,
    task_slug: str,
    branch: str,
    ordered_repos: list[str],
    repo_paths: dict[str, Path],
    repo_deps: dict[str, list[str]],
) -> Path:
    """Generate a handoff manifest YAML file."""
    handoffs_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for i, repo in enumerate(ordered_repos, 1):
        entries.append(
            MergeOrderEntry(
                order=i,
                repo=repo,
                path=repo_paths[repo],
                branch=branch,
                depends_on=repo_deps.get(repo, []),
            )
        )

    manifest = HandoffManifest(
        task=task_slug,
        branch=branch,
        generated_at=datetime.now(timezone.utc),
        merge_order=entries,
    )

    path = handoffs_dir / f"{task_slug}.yaml"
    data = manifest.model_dump(mode="json")
    # Convert Path objects to strings
    for entry in data["merge_order"]:
        entry["path"] = str(entry["path"])
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_handoff.py -v`
Expected: All 2 tests PASS.

- [ ] **Step 5: Add `--handoff` flag to `finish` command**

Modify `src/mship/cli/worktree.py` — update the `finish` function:

```python
    @app.command()
    def finish(
        handoff: bool = typer.Option(False, "--handoff", help="Generate CI handoff manifest"),
    ):
        """Create PRs and clean up worktrees in dependency order."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task to finish")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        graph = container.graph()
        config = container.config()
        ordered = graph.topo_sort(task.affected_repos)

        if handoff:
            from mship.core.handoff import generate_handoff

            state_dir = container.state_dir()
            repo_paths = {name: config.repos[name].path for name in ordered}
            repo_deps = {name: config.repos[name].depends_on for name in ordered}
            path = generate_handoff(
                handoffs_dir=Path(state_dir) / "handoffs",
                task_slug=task.slug,
                branch=task.branch,
                ordered_repos=ordered,
                repo_paths=repo_paths,
                repo_deps=repo_deps,
            )
            if output.is_tty:
                output.success(f"Handoff manifest written to: {path}")
            else:
                output.json({"handoff": str(path), "task": task.slug})
            return

        if output.is_tty:
            output.print(f"[bold]Finishing task:[/bold] {task.slug}")
            output.print(f"[bold]Merge order:[/bold]")
            for i, repo in enumerate(ordered, 1):
                output.print(f"  {i}. {repo}")
        else:
            output.json({
                "task": task.slug,
                "merge_order": ordered,
                "status": "manual_pr_required",
            })

        output.warning(
            "PR creation not yet implemented in v1. "
            "Use `gh pr create` manually in each repo in the order shown above."
        )
```

- [ ] **Step 6: Add CLI test for `--handoff`**

Add to `tests/cli/test_worktree.py`:

```python
def test_finish_handoff(configured_git_app: Path):
    runner.invoke(app, ["spawn", "handoff test", "--repos", "shared,auth-service"])
    result = runner.invoke(app, ["finish", "--handoff"])
    assert result.exit_code == 0
    handoff_file = configured_git_app / ".mothership" / "handoffs" / "handoff-test.yaml"
    assert handoff_file.exists()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_worktree.py -v`
Expected: All tests PASS (existing + new).

- [ ] **Step 8: Commit**

```bash
git add src/mship/core/handoff.py tests/core/test_handoff.py src/mship/cli/worktree.py tests/cli/test_worktree.py
git commit -m "feat: add CI/CD handoff manifest generation with mship finish --handoff"
```

---

### Task 7: PruneManager Core

**Files:**
- Create: `src/mship/core/prune.py`
- Create: `tests/core/test_prune.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_prune.py`:
```python
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.config import ConfigLoader
from mship.core.prune import PruneManager, OrphanedWorktree
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.git import GitRunner


@pytest.fixture
def prune_deps(workspace_with_git: Path):
    workspace = workspace_with_git
    config = ConfigLoader.load(workspace / "mothership.yaml")
    state_dir = workspace / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    return config, state_mgr, git, workspace


def test_scan_no_orphans(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    assert orphans == []


def test_scan_finds_disk_orphan(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    # Create a worktree manually without recording in state
    shared_path = workspace / "shared"
    wt_path = shared_path / ".worktrees" / "feat" / "orphan"
    git.worktree_add(repo_path=shared_path, worktree_path=wt_path, branch="feat/orphan")

    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    assert len(orphans) == 1
    assert orphans[0].reason == "not_in_state"
    assert "shared" in orphans[0].repo


def test_scan_finds_state_orphan(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    # Create a state entry pointing to nonexistent worktree
    task = Task(
        slug="ghost",
        description="Ghost task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/ghost",
        worktrees={"shared": Path("/tmp/nonexistent/worktree")},
    )
    state = WorkspaceState(current_task="ghost", tasks={"ghost": task})
    state_mgr.save(state)

    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    assert any(o.reason == "not_on_disk" for o in orphans)


def test_prune_removes_disk_orphan(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    shared_path = workspace / "shared"
    wt_path = shared_path / ".worktrees" / "feat" / "orphan"
    git.worktree_add(repo_path=shared_path, worktree_path=wt_path, branch="feat/orphan")

    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    count = mgr.prune(orphans)
    assert count == 1
    assert not wt_path.exists()


def test_prune_removes_state_orphan(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    task = Task(
        slug="ghost",
        description="Ghost task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/ghost",
        worktrees={"shared": Path("/tmp/nonexistent/worktree")},
    )
    state = WorkspaceState(current_task="ghost", tasks={"ghost": task})
    state_mgr.save(state)

    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    count = mgr.prune(orphans)
    assert count >= 1
    state = state_mgr.load()
    assert "ghost" not in state.tasks
    assert state.current_task is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_prune.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

`src/mship/core/prune.py`:
```python
from dataclasses import dataclass
from pathlib import Path

from mship.core.config import WorkspaceConfig
from mship.core.state import StateManager
from mship.util.git import GitRunner


@dataclass
class OrphanedWorktree:
    repo: str
    path: Path
    reason: str  # "not_in_state" | "not_on_disk"


class PruneManager:
    """Detect and clean up orphaned worktrees."""

    def __init__(
        self,
        config: WorkspaceConfig,
        state_manager: StateManager,
        git: GitRunner,
    ) -> None:
        self._config = config
        self._state_manager = state_manager
        self._git = git

    def scan(self) -> list[OrphanedWorktree]:
        orphans: list[OrphanedWorktree] = []

        # Collect all worktree paths tracked in state
        state = self._state_manager.load()
        tracked_paths: set[str] = set()
        for task in state.tasks.values():
            for repo_name, wt_path in task.worktrees.items():
                tracked_paths.add(str(Path(wt_path).resolve()))

        # Scan filesystem for worktrees not in state
        for repo_name, repo_config in self._config.repos.items():
            worktrees_dir = repo_config.path / ".worktrees"
            if not worktrees_dir.exists():
                continue
            for wt_path in self._walk_worktrees(worktrees_dir):
                resolved = str(wt_path.resolve())
                if resolved not in tracked_paths:
                    orphans.append(OrphanedWorktree(
                        repo=repo_name,
                        path=wt_path,
                        reason="not_in_state",
                    ))

        # Check state entries pointing to nonexistent worktrees
        for task_slug, task in state.tasks.items():
            for repo_name, wt_path in task.worktrees.items():
                if not Path(wt_path).exists():
                    orphans.append(OrphanedWorktree(
                        repo=repo_name,
                        path=Path(wt_path),
                        reason="not_on_disk",
                    ))

        return orphans

    def prune(self, orphans: list[OrphanedWorktree]) -> int:
        pruned = 0
        state = self._state_manager.load()
        state_changed = False

        for orphan in orphans:
            if orphan.reason == "not_in_state":
                # Remove worktree from disk
                repo_config = self._config.repos.get(orphan.repo)
                if repo_config:
                    try:
                        self._git.worktree_remove(
                            repo_path=repo_config.path,
                            worktree_path=orphan.path,
                        )
                    except Exception:
                        # Force remove if git worktree remove fails
                        import shutil
                        shutil.rmtree(orphan.path, ignore_errors=True)
                    # Try to clean up the branch
                    try:
                        # Extract branch name from worktree path
                        self._git._run_git_worktree_prune(repo_config.path)
                    except Exception:
                        pass
                pruned += 1

            elif orphan.reason == "not_on_disk":
                # Remove task entry from state if all its worktrees are gone
                for task_slug, task in list(state.tasks.items()):
                    if orphan.repo in task.worktrees:
                        wt_path = task.worktrees[orphan.repo]
                        if not Path(wt_path).exists():
                            del state.tasks[task_slug]
                            if state.current_task == task_slug:
                                state.current_task = None
                            state_changed = True
                            pruned += 1
                            break

        if state_changed:
            self._state_manager.save(state)

        # Run git worktree prune per repo
        for repo_config in self._config.repos.values():
            self._git._run_git_worktree_prune(repo_config.path)

        return pruned

    def _walk_worktrees(self, worktrees_dir: Path) -> list[Path]:
        """Find worktree directories (contain a .git file)."""
        results: list[Path] = []
        for item in worktrees_dir.rglob(".git"):
            if item.is_file():  # worktrees have a .git file, not directory
                results.append(item.parent)
        return results
```

- [ ] **Step 4: Add `_run_git_worktree_prune` to GitRunner**

Modify `src/mship/util/git.py` — add method:

```python
    def _run_git_worktree_prune(self, repo_path: Path) -> None:
        """Clean up stale git worktree tracking."""
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_path,
            capture_output=True,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_prune.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/prune.py tests/core/test_prune.py src/mship/util/git.py
git commit -m "feat: add PruneManager for orphaned worktree detection and cleanup"
```

---

### Task 8: Prune CLI Command & Container Wiring

**Files:**
- Create: `src/mship/cli/prune.py`
- Create: `tests/cli/test_prune.py`
- Modify: `src/mship/container.py`
- Modify: `src/mship/cli/__init__.py`

- [ ] **Step 1: Add PruneManager to container**

Modify `src/mship/container.py` — add import and provider:

```python
from mship.core.prune import PruneManager

# Inside Container class:
    prune_manager = providers.Factory(
        PruneManager,
        config=config,
        state_manager=state_manager,
        git=git,
    )
```

- [ ] **Step 2: Write the failing tests**

`tests/cli/test_prune.py`:
```python
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.util.git import GitRunner

runner = CliRunner()


@pytest.fixture
def configured_prune_app(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)
    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_prune_dry_run_no_orphans(configured_prune_app: Path):
    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 0
    assert "No orphaned worktrees" in result.output


def test_prune_dry_run_finds_orphan(configured_prune_app: Path):
    git = GitRunner()
    shared_path = configured_prune_app / "shared"
    wt_path = shared_path / ".worktrees" / "feat" / "orphan"
    git.worktree_add(repo_path=shared_path, worktree_path=wt_path, branch="feat/orphan")

    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 0
    assert "orphan" in result.output.lower() or "not_in_state" in result.output
    # Dry run — worktree should still exist
    assert wt_path.exists()


def test_prune_force_removes_orphan(configured_prune_app: Path):
    git = GitRunner()
    shared_path = configured_prune_app / "shared"
    wt_path = shared_path / ".worktrees" / "feat" / "orphan"
    git.worktree_add(repo_path=shared_path, worktree_path=wt_path, branch="feat/orphan")

    result = runner.invoke(app, ["prune", "--force"])
    assert result.exit_code == 0
    assert not wt_path.exists()
```

- [ ] **Step 3: Write the implementation**

`src/mship/cli/prune.py`:
```python
import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def prune(
        force: bool = typer.Option(False, "--force", help="Actually remove orphaned worktrees"),
    ):
        """Find and clean up orphaned worktrees."""
        container = get_container()
        output = Output()
        prune_mgr = container.prune_manager()

        orphans = prune_mgr.scan()

        if not orphans:
            if output.is_tty:
                output.success("No orphaned worktrees found")
            else:
                output.json({"orphans": [], "pruned": False})
            return

        if not force:
            if output.is_tty:
                output.warning(f"Found {len(orphans)} orphaned worktree(s):")
                for o in orphans:
                    output.print(f"  {o.repo}: {o.path} ({o.reason})")
                output.print("\nRun `mship prune --force` to clean up.")
            else:
                output.json({
                    "orphans": [
                        {"repo": o.repo, "path": str(o.path), "reason": o.reason}
                        for o in orphans
                    ],
                    "pruned": False,
                })
            return

        count = prune_mgr.prune(orphans)
        if output.is_tty:
            output.success(f"Pruned {count} item(s)")
        else:
            output.json({"pruned": True, "count": count})
```

- [ ] **Step 4: Register in `cli/__init__.py`**

Add to `src/mship/cli/__init__.py`:

```python
from mship.cli import prune as _prune_mod

_prune_mod.register(app, get_container)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_prune.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/mship/cli/prune.py src/mship/container.py src/mship/cli/__init__.py tests/cli/test_prune.py
git commit -m "feat: add mship prune command for worktree garbage collection"
```

---

### Task 9: Integration Test for Agent Resilience Features

**Files:**
- Create: `tests/test_resilience_integration.py`

- [ ] **Step 1: Write the integration test**

`tests/test_resilience_integration.py`:
```python
"""Integration test: spawn → block → log → unblock → phase → finish --handoff."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def full_workspace(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_agent_resilience_lifecycle(full_workspace: Path):
    # 1. Spawn task
    result = runner.invoke(app, ["spawn", "resilience test", "--repos", "shared,auth-service"])
    assert result.exit_code == 0, result.output

    # 2. Log context
    result = runner.invoke(app, ["log", "Starting work on auth controller"])
    assert result.exit_code == 0

    # 3. Phase to dev
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 0

    # 4. Block
    result = runner.invoke(app, ["block", "waiting on API key"])
    assert result.exit_code == 0

    # 5. Status shows blocked
    result = runner.invoke(app, ["status"])
    assert "BLOCKED" in result.output
    assert "waiting on API key" in result.output

    # 6. Read log — should show spawn, phase, block events
    result = runner.invoke(app, ["log"])
    assert "Task spawned" in result.output
    assert "Phase transition" in result.output
    assert "Blocked: waiting on API key" in result.output

    # 7. Unblock
    result = runner.invoke(app, ["unblock"])
    assert result.exit_code == 0

    # 8. Status no longer blocked
    result = runner.invoke(app, ["status"])
    assert "BLOCKED" not in result.output

    # 9. Generate handoff
    result = runner.invoke(app, ["finish", "--handoff"])
    assert result.exit_code == 0
    handoff_file = full_workspace / ".mothership" / "handoffs" / "resilience-test.yaml"
    assert handoff_file.exists()
    with open(handoff_file) as f:
        data = yaml.safe_load(f)
    assert data["task"] == "resilience-test"
    assert len(data["merge_order"]) == 2

    # 10. Abort (cleanup)
    result = runner.invoke(app, ["abort", "--yes"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_resilience_integration.py -v`
Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_resilience_integration.py
git commit -m "test: add integration test for agent resilience features"
```

---

## Self-Review

**Spec coverage:**
- Blocked state overlay: Task 1 (model), Task 4 (CLI + status)
- Auto-unblock on phase transition: Task 3 (PhaseManager)
- Task log (LogManager): Task 2 (core), Task 5 (CLI)
- Auto-logging lifecycle events: Task 3 (spawn, phase, block/unblock)
- Handoff manifest: Task 6 (core model + CLI flag)
- Prune (scan + force): Task 7 (core), Task 8 (CLI)
- Integration test: Task 9

**Placeholder scan:** No TBDs or TODOs. All steps have complete code.

**Type consistency:** `LogEntry`, `LogManager`, `OrphanedWorktree`, `PruneManager`, `HandoffManifest`, `MergeOrderEntry` — all consistent across tasks. `blocked_reason`/`blocked_at` field names match between state model, CLI, and status display.
