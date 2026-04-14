# Test Diff + Structured Logs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `mship test --diff` with per-iteration run capture + labeled cross-run diff, and upgrade `LogEntry` with structured fields (`repo`, `iteration`, `test_state`, `action`, `open_question`) so agents get actionable state instead of stdout.

**Architecture:** New `core/test_history.py` module owns iteration-file I/O and diff computation. `LogEntry` gains optional fields + a permissive header parser that round-trips both old and new formats. `mship test` records a timed run, writes the iteration file, computes the diff, auto-appends a structured log entry. `mship log` grows flags for the new fields with inference from `task.active_repo` and `task.test_iteration`. `mship switch`'s handoff filters `last_log_in_repo` by repo tag when available.

**Tech Stack:** Python 3.12+, Typer, Pydantic, existing `ShellRunner` / `StateManager` / `LogManager` / `RepoExecutor`.

**Spec:** `docs/superpowers/specs/2026-04-14-test-diff-structured-logs-design.md`

---

## File Structure

**Create:**
- `src/mship/core/test_history.py` — iteration file I/O, diff computation, pruning.
- `tests/core/test_test_history.py`.
- `tests/test_test_diff_integration.py` (integration).

**Modify:**
- `src/mship/core/log.py` — 5 new fields on `LogEntry`; permissive header parser; `append()` kwargs.
- `src/mship/core/state.py` — `Task.test_iteration: int = 0`.
- `src/mship/core/executor.py` — `RepoResult.duration_ms`; time each repo's run.
- `src/mship/cli/log.py` — new flags + inference + `--show-open`.
- `src/mship/cli/exec.py` — `mship test` writes run, renders diff, auto-logs.
- `src/mship/core/switch.py` — `last_log_in_repo` prefers repo-tagged entries.
- `tests/core/test_log.py` — extend.
- `skills/working-with-mothership/SKILL.md`, `README.md` — document new flags + output.

---

## Task 1: `LogEntry` structured fields + permissive parser

**Files:**
- Modify: `src/mship/core/log.py`
- Test: `tests/core/test_log.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_log.py`:
```python
from datetime import datetime, timezone

from mship.core.log import LogEntry, LogManager


def test_log_entry_defaults_to_none_for_new_fields():
    e = LogEntry(timestamp=datetime.now(timezone.utc), message="m")
    assert e.repo is None
    assert e.iteration is None
    assert e.test_state is None
    assert e.action is None
    assert e.open_question is None


def test_parse_old_format_entry(tmp_path):
    path = tmp_path / "old.md"
    path.write_text(
        "# Task Log: old\n\n"
        "## 2026-04-14T12:00:00Z\nlegacy message\n"
    )
    log_mgr = LogManager(tmp_path)
    # Create the matching task file name
    (tmp_path / "old.md").write_text(
        "# Task Log: old\n\n"
        "## 2026-04-14T12:00:00Z\nlegacy message\n"
    )
    entries = log_mgr.read("old")
    assert len(entries) == 1
    assert entries[0].message == "legacy message"
    assert entries[0].repo is None
    assert entries[0].iteration is None


def test_parse_new_format_entry(tmp_path):
    (tmp_path / "t.md").write_text(
        "# Task Log: t\n\n"
        "## 2026-04-14T12:00:00Z  repo=shared  iter=3  test=pass  action=implementing\n"
        "Implemented Label type\n"
    )
    log_mgr = LogManager(tmp_path)
    entries = log_mgr.read("t")
    assert len(entries) == 1
    e = entries[0]
    assert e.message == "Implemented Label type"
    assert e.repo == "shared"
    assert e.iteration == 3
    assert e.test_state == "pass"
    assert e.action == "implementing"


def test_parse_quoted_value_with_spaces(tmp_path):
    (tmp_path / "t.md").write_text(
        "# Task Log: t\n\n"
        '## 2026-04-14T12:00:00Z  open="how to handle null workspace"  repo=auth\n'
        "Stuck\n"
    )
    log_mgr = LogManager(tmp_path)
    (entry,) = log_mgr.read("t")
    assert entry.open_question == "how to handle null workspace"
    assert entry.repo == "auth"


def test_parse_mixed_old_and_new_entries(tmp_path):
    (tmp_path / "t.md").write_text(
        "# Task Log: t\n\n"
        "## 2026-04-14T12:00:00Z\nlegacy\n\n"
        "## 2026-04-14T12:05:00Z  repo=shared\nstructured\n"
    )
    log_mgr = LogManager(tmp_path)
    entries = log_mgr.read("t")
    assert len(entries) == 2
    assert entries[0].message == "legacy"
    assert entries[0].repo is None
    assert entries[1].message == "structured"
    assert entries[1].repo == "shared"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_log.py -v -k "defaults_to_none or parse_old or parse_new or parse_quoted or parse_mixed"`
Expected: FAIL — new fields missing, parser doesn't handle kv headers.

- [ ] **Step 3: Implement**

Replace `src/mship/core/log.py` with:
```python
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional


TestState = Literal["pass", "fail", "mixed"]


@dataclass
class LogEntry:
    timestamp: datetime
    message: str
    repo: Optional[str] = None
    iteration: Optional[int] = None
    test_state: Optional[TestState] = None
    action: Optional[str] = None
    open_question: Optional[str] = None


_HEADER_RE = re.compile(
    r"^## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)(?P<kv>(?:  [^\n]*)?)\n(.*?)(?=\n## \d{4}-\d{2}-\d{2}T|\Z)",
    re.MULTILINE | re.DOTALL,
)

_KV_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')


def _parse_kv(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _KV_RE.finditer(raw):
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else m.group(3)
        out[key] = val
    return out


def _format_kv(entry: LogEntry) -> str:
    parts: list[str] = []
    if entry.repo is not None:
        parts.append(f"repo={entry.repo}")
    if entry.iteration is not None:
        parts.append(f"iter={entry.iteration}")
    if entry.test_state is not None:
        parts.append(f"test={entry.test_state}")
    if entry.action is not None:
        parts.append(f"action={shlex.quote(entry.action) if ' ' in entry.action else entry.action}")
    if entry.open_question is not None:
        q = entry.open_question.replace('"', '\\"')
        parts.append(f'open="{q}"')
    return "  " + "  ".join(parts) if parts else ""


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

    def append(
        self,
        task_slug: str,
        message: str,
        *,
        repo: Optional[str] = None,
        iteration: Optional[int] = None,
        test_state: Optional[TestState] = None,
        action: Optional[str] = None,
        open_question: Optional[str] = None,
    ) -> None:
        path = self._log_path(task_slug)
        if not path.exists():
            self.create(task_slug)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            message=message,
            repo=repo,
            iteration=iteration,
            test_state=test_state,
            action=action,
            open_question=open_question,
        )
        kv = _format_kv(entry)
        with open(path, "a") as f:
            f.write(f"\n## {timestamp}{kv}\n{message}\n")

    def read(self, task_slug: str, last: Optional[int] = None) -> list[LogEntry]:
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
        for match in _HEADER_RE.finditer(content):
            timestamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            kv_raw = match.group("kv") or ""
            message = match.group(3).strip()
            if not message:
                continue
            kv = _parse_kv(kv_raw)
            iteration = int(kv["iter"]) if "iter" in kv and kv["iter"].isdigit() else None
            entries.append(LogEntry(
                timestamp=timestamp,
                message=message,
                repo=kv.get("repo"),
                iteration=iteration,
                test_state=kv.get("test"),
                action=kv.get("action"),
                open_question=kv.get("open"),
            ))
        return entries
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_log.py -v`
Expected: PASS (all new + existing tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/log.py tests/core/test_log.py
git commit -m "feat(log): structured LogEntry fields with permissive kv-header parser"
```

---

## Task 2: `Task.test_iteration` + executor `duration_ms`

**Files:**
- Modify: `src/mship/core/state.py`
- Modify: `src/mship/core/executor.py`
- Test: `tests/core/test_state.py`
- Test: `tests/core/test_executor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_state.py`:
```python
def test_task_defaults_test_iteration_to_zero():
    from datetime import datetime, timezone
    from mship.core.state import Task
    task = Task(
        slug="t", description="d", phase="plan",
        created_at=datetime.now(timezone.utc),
        affected_repos=["a"], branch="feat/t",
    )
    assert task.test_iteration == 0
```

Append to `tests/core/test_executor.py`:
```python
def test_repo_result_has_duration_ms_default():
    from mship.core.executor import RepoResult
    from mship.util.shell import ShellResult
    r = RepoResult(
        repo="x", task_name="test",
        shell_result=ShellResult(returncode=0, stdout="", stderr=""),
    )
    assert r.duration_ms == 0


def test_executor_records_duration_ms_on_test_run(workspace_with_git, monkeypatch):
    """Each repo result should carry a non-zero duration_ms after a test run."""
    from mship.container import Container
    from mship.core.state import StateManager
    from pathlib import Path

    container = Container()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        executor = container.executor()
        result = executor.execute("test", repos=["shared"], run_all=False)
        assert result.results
        assert all(r.duration_ms >= 0 for r in result.results)
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_state.py tests/core/test_executor.py -v -k "test_iteration or duration_ms"`
Expected: FAIL — field not defined, attribute missing.

- [ ] **Step 3: Add `Task.test_iteration`**

In `src/mship/core/state.py`, extend `Task`:
```python
class Task(BaseModel):
    ...existing fields...
    active_repo: str | None = None
    last_switched_at_sha: dict[str, dict[str, str]] = {}
    test_iteration: int = 0
```

- [ ] **Step 4: Add `duration_ms` + timing**

In `src/mship/core/executor.py`:

1. Add `duration_ms: int = 0` to `RepoResult`:
```python
@dataclass
class RepoResult:
    repo: str
    task_name: str
    shell_result: ShellResult
    skipped: bool = False
    background_pid: int | None = None
    healthcheck: HealthcheckResult | None = None
    duration_ms: int = 0
```

2. Find `_execute_one` (around line 114). Locate the `shell_result = self._shell.run_task(...)` call on the non-background path. Wrap it with a monotonic timer and set `duration_ms` on the returned `RepoResult`. The exact snippet depends on the current layout; update the non-background branch to read:

```python
import time as _time
_start = _time.monotonic()
shell_result = self._shell.run_task(
    task_name=canonical_task,
    actual_task_name=actual_name,
    cwd=cwd,
    env_runner=env_runner,
    env=upstream_env or None,
)
_elapsed_ms = int((_time.monotonic() - _start) * 1000)

return (
    RepoResult(
        repo=repo_name,
        task_name=actual_name,
        shell_result=shell_result,
        duration_ms=_elapsed_ms,
    ),
    None,
)
```

Keep the `start_mode == "background"` branch unchanged (duration is meaningless for long-running processes; leaves it at default 0).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/core/test_state.py tests/core/test_executor.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/state.py src/mship/core/executor.py \
        tests/core/test_state.py tests/core/test_executor.py
git commit -m "feat: add Task.test_iteration and RepoResult.duration_ms"
```

---

## Task 3: `core/test_history.py` — iteration I/O + diff + prune

**Files:**
- Create: `src/mship/core/test_history.py`
- Test: `tests/core/test_test_history.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_test_history.py`:
```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.test_history import (
    write_run, read_run, latest_iteration,
    compute_diff, prune,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / ".mothership"
    d.mkdir()
    return d


def _results(**kw):
    """Helper: build the per-repo results dict."""
    return {
        name: {"status": status, "duration_ms": dur, "exit_code": ec, "stderr_tail": tail}
        for name, (status, dur, ec, tail) in kw.items()
    }


def test_write_run_creates_iteration_file_and_latest_pointer(state_dir):
    results = _results(shared=("pass", 1200, 0, None))
    path = write_run(
        state_dir, "t", iteration=1,
        started_at=datetime(2026, 4, 14, 12, tzinfo=timezone.utc),
        duration_ms=1234, results=results,
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["iteration"] == 1
    assert data["duration_ms"] == 1234
    assert data["repos"]["shared"]["status"] == "pass"
    # latest pointer
    latest = state_dir / "test-runs" / "t" / "latest.json"
    assert latest.exists()
    assert json.loads(latest.read_text())["iteration"] == 1


def test_read_run_round_trip(state_dir):
    write_run(
        state_dir, "t", iteration=2,
        started_at=datetime(2026, 4, 14, 12, tzinfo=timezone.utc),
        duration_ms=100,
        results=_results(api=("fail", 99, 1, "boom")),
    )
    run = read_run(state_dir, "t", 2)
    assert run is not None
    assert run["repos"]["api"]["stderr_tail"] == "boom"


def test_read_run_missing_returns_none(state_dir):
    assert read_run(state_dir, "t", 42) is None


def test_latest_iteration_returns_highest(state_dir):
    for i in (1, 2, 3):
        write_run(state_dir, "t", iteration=i,
                   started_at=datetime.now(timezone.utc),
                   duration_ms=0, results=_results())
    assert latest_iteration(state_dir, "t") == 3


def test_latest_iteration_none_for_missing_task(state_dir):
    assert latest_iteration(state_dir, "t") is None


def test_compute_diff_first_run_tags_all_as_first_run():
    current = {"iteration": 1, "repos": {"a": {"status": "pass"}}}
    result = compute_diff(current, previous=None, pre_previous=None)
    assert result["previous_iteration"] is None
    assert result["tags"] == {"a": "first run"}
    assert result["summary"]["new_failures"] == []


def test_compute_diff_pass_to_pass_is_still_passing():
    prev = {"iteration": 1, "repos": {"a": {"status": "pass"}}}
    curr = {"iteration": 2, "repos": {"a": {"status": "pass"}}}
    d = compute_diff(curr, prev, None)
    assert d["tags"]["a"] == "still passing"


def test_compute_diff_pass_to_fail_is_new_failure_without_pre_previous():
    prev = {"iteration": 1, "repos": {"a": {"status": "pass"}}}
    curr = {"iteration": 2, "repos": {"a": {"status": "fail"}}}
    d = compute_diff(curr, prev, None)
    assert d["tags"]["a"] == "new failure"
    assert d["summary"]["new_failures"] == ["a"]


def test_compute_diff_pass_to_fail_is_regression_when_pre_previous_also_passed():
    pre = {"iteration": 1, "repos": {"a": {"status": "pass"}}}
    prev = {"iteration": 2, "repos": {"a": {"status": "pass"}}}
    curr = {"iteration": 3, "repos": {"a": {"status": "fail"}}}
    d = compute_diff(curr, prev, pre)
    assert d["tags"]["a"] == "regression"
    assert d["summary"]["regressions"] == ["a"]


def test_compute_diff_fail_to_pass_is_fix():
    prev = {"iteration": 1, "repos": {"a": {"status": "fail"}}}
    curr = {"iteration": 2, "repos": {"a": {"status": "pass"}}}
    d = compute_diff(curr, prev, None)
    assert d["tags"]["a"] == "fix"
    assert d["summary"]["fixes"] == ["a"]


def test_compute_diff_fail_to_fail_is_still_failing():
    prev = {"iteration": 1, "repos": {"a": {"status": "fail"}}}
    curr = {"iteration": 2, "repos": {"a": {"status": "fail"}}}
    d = compute_diff(curr, prev, None)
    assert d["tags"]["a"] == "still failing"


def test_prune_keeps_newest_n(state_dir):
    for i in range(1, 26):
        write_run(state_dir, "t", iteration=i,
                   started_at=datetime.now(timezone.utc),
                   duration_ms=0, results=_results())
    prune(state_dir, "t", keep=20)
    remaining = sorted(
        int(p.stem) for p in (state_dir / "test-runs" / "t").iterdir()
        if p.stem.isdigit()
    )
    assert remaining == list(range(6, 26))
    # latest.json preserved
    assert (state_dir / "test-runs" / "t" / "latest.json").exists()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_test_history.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

`src/mship/core/test_history.py`:
```python
"""Per-iteration test run storage + diffing."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _run_dir(state_dir: Path, task_slug: str) -> Path:
    return state_dir / "test-runs" / task_slug


def _run_path(state_dir: Path, task_slug: str, iteration: int) -> Path:
    return _run_dir(state_dir, task_slug) / f"{iteration}.json"


def _latest_path(state_dir: Path, task_slug: str) -> Path:
    return _run_dir(state_dir, task_slug) / "latest.json"


def write_run(
    state_dir: Path,
    task_slug: str,
    iteration: int,
    started_at: datetime,
    duration_ms: int,
    results: dict[str, dict[str, Any]],
) -> Path:
    """Write iteration JSON + update latest pointer. Returns the iteration file path."""
    run_dir = _run_dir(state_dir, task_slug)
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "iteration": iteration,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "duration_ms": duration_ms,
        "repos": results,
    }
    path = _run_path(state_dir, task_slug, iteration)
    path.write_text(json.dumps(payload, indent=2))
    _latest_path(state_dir, task_slug).write_text(json.dumps(payload, indent=2))
    return path


def read_run(state_dir: Path, task_slug: str, iteration: int) -> dict | None:
    path = _run_path(state_dir, task_slug, iteration)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def latest_iteration(state_dir: Path, task_slug: str) -> int | None:
    d = _run_dir(state_dir, task_slug)
    if not d.exists():
        return None
    numbers = [int(p.stem) for p in d.iterdir() if p.stem.isdigit()]
    return max(numbers) if numbers else None


def compute_diff(
    current: dict,
    previous: dict | None,
    pre_previous: dict | None,
) -> dict:
    """Label each repo based on pass/fail transitions."""
    tags: dict[str, str] = {}
    summary = {
        "new_failures": [],
        "fixes": [],
        "regressions": [],
        "new_passes": [],
    }
    prev_repos = (previous or {}).get("repos", {})
    pre_prev_repos = (pre_previous or {}).get("repos", {})

    for name, r in current.get("repos", {}).items():
        cur_status = r.get("status")
        if previous is None or name not in prev_repos:
            tags[name] = "first run"
            continue
        prev_status = prev_repos[name].get("status")
        if prev_status == "pass" and cur_status == "pass":
            tags[name] = "still passing"
        elif prev_status == "fail" and cur_status == "pass":
            tags[name] = "fix"
            summary["fixes"].append(name)
        elif prev_status == "pass" and cur_status == "fail":
            pre_status = pre_prev_repos.get(name, {}).get("status")
            if pre_status == "pass":
                tags[name] = "regression"
                summary["regressions"].append(name)
            else:
                tags[name] = "new failure"
                summary["new_failures"].append(name)
        elif prev_status == "fail" and cur_status == "fail":
            tags[name] = "still failing"
        else:
            tags[name] = "changed"

    return {
        "previous_iteration": (previous or {}).get("iteration"),
        "tags": tags,
        "summary": summary,
    }


def prune(state_dir: Path, task_slug: str, keep: int = 20) -> None:
    d = _run_dir(state_dir, task_slug)
    if not d.exists():
        return
    numbered = sorted(
        (int(p.stem), p) for p in d.iterdir() if p.stem.isdigit()
    )
    if len(numbered) <= keep:
        return
    for _, path in numbered[:-keep]:
        path.unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/core/test_test_history.py -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/test_history.py tests/core/test_test_history.py
git commit -m "feat(test_history): iteration storage, diff, prune"
```

---

## Task 4: `mship test` integration — write run, diff, auto-log, `--no-diff`

**Files:**
- Modify: `src/mship/cli/exec.py` (the `test_cmd` around line 38)
- Test: `tests/test_test_diff_integration.py` (new)

- [ ] **Step 1: Write the failing integration tests**

`tests/test_test_diff_integration.py`:
```python
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager


runner = CliRunner()


@pytest.fixture
def test_workspace(workspace_with_git):
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def _spawn(description="first"):
    result = runner.invoke(
        app, ["spawn", description, "--repos", "shared", "--force-audit"],
    )
    assert result.exit_code == 0, result.output


def test_first_test_run_writes_iteration_file_and_log_entry(test_workspace):
    _spawn()
    result = runner.invoke(app, ["test"])
    # Test may pass or fail depending on Taskfile stub; either exit code accepted.
    # The important thing: iteration file and log entry exist.
    task_slug = "first"
    iter_path = test_workspace / ".mothership" / "test-runs" / task_slug / "1.json"
    assert iter_path.exists(), result.output
    data = json.loads(iter_path.read_text())
    assert data["iteration"] == 1
    assert "shared" in data["repos"]

    state = StateManager(test_workspace / ".mothership").load()
    assert state.tasks[task_slug].test_iteration == 1

    # Auto-appended log entry
    from mship.core.log import LogManager
    log_mgr = LogManager(test_workspace / ".mothership" / "logs")
    entries = log_mgr.read(task_slug)
    assert any(e.action == "ran tests" and e.iteration == 1 for e in entries)


def test_second_test_run_shows_still_passing_or_still_failing(test_workspace):
    _spawn("second")
    r1 = runner.invoke(app, ["test"])
    r2 = runner.invoke(app, ["test"])
    # One of the labels must appear in r2 output (TTY or not; CliRunner is non-TTY so JSON).
    try:
        payload = json.loads(r2.output)
        tags = payload["diff"]["tags"]
        assert "shared" in tags
        assert tags["shared"] in {"still passing", "still failing"}
    except json.JSONDecodeError:
        assert ("still passing" in r2.output) or ("still failing" in r2.output)


def test_no_diff_flag_suppresses_diff(test_workspace):
    _spawn("nodiff")
    runner.invoke(app, ["test"])
    result = runner.invoke(app, ["test", "--no-diff"])
    # No tags section, no "still passing" / "new failure" / etc. in either plain or JSON mode.
    try:
        payload = json.loads(result.output)
        assert "diff" not in payload
    except json.JSONDecodeError:
        for label in ("still passing", "still failing", "new failure", "fix", "regression"):
            assert label not in result.output
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_test_diff_integration.py -v`
Expected: FAIL — iteration files not written, no `--no-diff` flag.

- [ ] **Step 3: Rewrite `test_cmd`**

In `src/mship/cli/exec.py`, replace the `test_cmd` function body. Keep the existing signature; replace behavior after the `_resolve_repos` call:

```python
    @app.command(name="test")
    def test_cmd(
        run_all: bool = typer.Option(False, "--all", help="Run all repos even on failure"),
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names to filter"),
        tag: Optional[list[str]] = typer.Option(None, "--tag", help="Filter repos by tag"),
        no_diff: bool = typer.Option(False, "--no-diff", help="Skip cross-run diff output"),
    ):
        """Run tests across affected repos; show diff vs. previous iteration."""
        from datetime import datetime, timezone
        from mship.core.test_history import (
            write_run, read_run, latest_iteration, compute_diff, prune,
        )

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn \"description\"` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        config = container.config()

        try:
            target_repos = _resolve_repos(config, task.affected_repos, repos, tag)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        state_dir = container.state_dir()
        prev_iter = latest_iteration(state_dir, task.slug)
        prev_run = read_run(state_dir, task.slug, prev_iter) if prev_iter else None
        pre_prev_run = (
            read_run(state_dir, task.slug, prev_iter - 1)
            if prev_iter and prev_iter > 1 else None
        )

        started_at = datetime.now(timezone.utc)

        executor = container.executor()
        result = executor.execute(
            "test", repos=target_repos, run_all=run_all,
            task_slug=state.current_task,
        )

        run_duration_ms = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )

        # Build per-repo results for the iteration file
        per_repo: dict[str, dict] = {}
        for r in result.results:
            status = "pass" if r.success else "fail"
            stderr_tail = None
            if status == "fail":
                stderr = (r.shell_result.stderr or "").splitlines()
                stderr_tail = "\n".join(stderr[-40:]) if stderr else None
            per_repo[r.repo] = {
                "status": status,
                "duration_ms": r.duration_ms,
                "exit_code": r.shell_result.returncode,
                "stderr_tail": stderr_tail,
            }

        new_iter = (prev_iter or 0) + 1
        write_run(
            state_dir, task.slug, iteration=new_iter,
            started_at=started_at, duration_ms=run_duration_ms,
            results=per_repo,
        )

        # Persist iteration on task
        task.test_iteration = new_iter
        state_mgr.save(state)

        prune(state_dir, task.slug, keep=20)

        # Summary for log entry
        pass_count = sum(1 for v in per_repo.values() if v["status"] == "pass")
        total = len(per_repo)
        if pass_count == total:
            test_state = "pass"
        elif pass_count == 0:
            test_state = "fail"
        else:
            test_state = "mixed"
        container.log_manager().append(
            task.slug,
            f"iter {new_iter}: {pass_count}/{total} passing",
            iteration=new_iter,
            test_state=test_state,
            action="ran tests",
        )

        # Render
        current_run = {
            "iteration": new_iter,
            "started_at": started_at.isoformat().replace("+00:00", "Z"),
            "duration_ms": run_duration_ms,
            "repos": per_repo,
        }
        diff = None if no_diff else compute_diff(current_run, prev_run, pre_prev_run)

        if output.is_tty:
            output.print(f"[bold]Test run #{new_iter}[/bold]  ({run_duration_ms / 1000:.1f}s)")
            for repo_name, info in per_repo.items():
                status = info["status"]
                color = "green" if status == "pass" else "red"
                dur_s = info["duration_ms"] / 1000
                line = f"  {repo_name}: [{color}]{status}[/{color}]  ({dur_s:.1f}s)"
                if diff and repo_name in diff["tags"]:
                    tag = diff["tags"][repo_name]
                    if tag in {"new failure", "regression", "fix"}:
                        line += f"  ← {tag}"
                output.print(line)
                if status == "fail" and info["stderr_tail"]:
                    for tline in info["stderr_tail"].splitlines()[-20:]:
                        output.print(f"    {tline}")
            if diff:
                prev_id = diff["previous_iteration"]
                new_fail = diff["summary"]["new_failures"]
                fixes = diff["summary"]["fixes"]
                parts = [f"{pass_count}/{total} repos passing"]
                if prev_id is not None and new_fail:
                    parts.append(f"{len(new_fail)} new failure(s) since iter #{prev_id}")
                if fixes:
                    parts.append(f"{len(fixes)} fix(es)")
                output.print("")
                output.print("  " + ". ".join(parts) + ".")
        else:
            payload = dict(current_run)
            if diff is not None:
                payload["diff"] = diff
            output.json(payload)

        if not result.success:
            raise typer.Exit(code=1)
```

If `_resolve_repos` or `Output` imports aren't already at the top of `exec.py`, leave them as they are (this task only touches the `test_cmd` body).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_test_diff_integration.py -v`
Expected: PASS (3 tests).

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/exec.py tests/test_test_diff_integration.py
git commit -m "feat(test): write iteration files, show diff vs previous run, auto-log"
```

---

## Task 5: `mship log` CLI — new flags + inference + `--show-open`

**Files:**
- Modify: `src/mship/cli/log.py`
- Test: `tests/cli/test_log.py` (create or extend)

- [ ] **Step 1: Write the failing tests**

Create or append to `tests/cli/test_log.py`:
```python
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.log import LogManager


runner = CliRunner()


def _setup(ws):
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(ws / ".mothership")


def _teardown():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def test_log_with_action_and_open_flags(workspace_with_git):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "flags test", "--repos", "shared", "--force-audit"])
        result = runner.invoke(
            app, ["log", "stuck",
                    "--action", "debugging middleware",
                    "--open", "how to handle null workspace",
                    "--repo", "shared",
                    "--test-state", "fail"],
        )
        assert result.exit_code == 0, result.output

        log_mgr = LogManager(workspace_with_git / ".mothership" / "logs")
        entries = log_mgr.read("flags-test")
        assert entries
        latest = entries[-1]
        assert latest.action == "debugging middleware"
        assert latest.open_question == "how to handle null workspace"
        assert latest.repo == "shared"
        assert latest.test_state == "fail"
    finally:
        _teardown()


def test_log_infers_repo_from_active_repo(workspace_with_git):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "infer test", "--repos", "shared", "--force-audit"])
        runner.invoke(app, ["switch", "shared"])
        runner.invoke(app, ["log", "did a thing"])
        log_mgr = LogManager(workspace_with_git / ".mothership" / "logs")
        entries = log_mgr.read("infer-test")
        did = next(e for e in entries if e.message == "did a thing")
        assert did.repo == "shared"
    finally:
        _teardown()


def test_log_show_open_lists_open_questions(workspace_with_git):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "open test", "--repos", "shared", "--force-audit"])
        runner.invoke(
            app, ["log", "stuck", "--open", "how to handle nulls", "--repo", "shared"],
        )
        runner.invoke(
            app, ["log", "also stuck", "--open", "timeout logic unclear", "--repo", "shared"],
        )
        result = runner.invoke(app, ["log", "--show-open"])
        assert result.exit_code == 0
        assert "how to handle nulls" in result.output
        assert "timeout logic unclear" in result.output
    finally:
        _teardown()


def test_log_show_open_empty_exits_zero(workspace_with_git):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "nothing open", "--repos", "shared", "--force-audit"])
        result = runner.invoke(app, ["log", "--show-open"])
        assert result.exit_code == 0
    finally:
        _teardown()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_log.py -v`
Expected: FAIL — flags don't exist.

- [ ] **Step 3: Rewrite `mship log`**

Replace `src/mship/cli/log.py`:
```python
from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(name="log")
    def log_cmd(
        message: Optional[str] = typer.Argument(None, help="Message to append to task log"),
        last: Optional[int] = typer.Option(None, "--last", help="Show only last N entries"),
        action: Optional[str] = typer.Option(None, "--action", help="Structured: what you were doing"),
        open_question: Optional[str] = typer.Option(None, "--open", help="Structured: blocking question"),
        test_state: Optional[str] = typer.Option(None, "--test-state", help="Structured: pass|fail|mixed"),
        repo: Optional[str] = typer.Option(None, "--repo", help="Structured: which repo this entry is about"),
        iteration: Optional[int] = typer.Option(None, "--iteration", help="Structured: iteration number"),
        no_repo: bool = typer.Option(False, "--no-repo", help="Suppress active-repo inference"),
        show_open: bool = typer.Option(False, "--show-open", help="List open questions from this task's log"),
    ):
        """Append to or read the current task's log."""
        from mship.util.duration import format_relative

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn \"description\"` to start one.")
            raise typer.Exit(code=1)

        log_mgr = container.log_manager()
        task = state.tasks[state.current_task]

        if show_open:
            entries = log_mgr.read(state.current_task)
            opens = [e for e in entries if e.open_question]
            if not opens:
                if output.is_tty:
                    output.print("(no open questions)")
                else:
                    output.json({"open_questions": []})
                return
            if output.is_tty:
                output.print("[bold]Open questions:[/bold]")
                for e in opens:
                    rel = format_relative(e.timestamp)
                    repo_prefix = f"{e.repo}: " if e.repo else ""
                    output.print(f"  [{rel}] {repo_prefix}{e.open_question}")
            else:
                output.json({"open_questions": [
                    {
                        "timestamp": e.timestamp.isoformat(),
                        "repo": e.repo,
                        "question": e.open_question,
                    }
                    for e in opens
                ]})
            return

        if message is not None:
            # Infer repo + iteration when not explicitly provided
            inferred_repo = repo
            if inferred_repo is None and not no_repo:
                inferred_repo = task.active_repo
            inferred_iter = iteration if iteration is not None else (
                task.test_iteration if task.test_iteration > 0 else None
            )
            log_mgr.append(
                state.current_task, message,
                repo=inferred_repo,
                iteration=inferred_iter,
                test_state=test_state,
                action=action,
                open_question=open_question,
            )
            if output.is_tty:
                output.success("Logged")
            else:
                output.json({"task": state.current_task, "logged": message})
            return

        # Read path (no message argument)
        entries = log_mgr.read(state.current_task, last=last)
        if not entries:
            output.print("No log entries")
            return
        if output.is_tty:
            for entry in entries:
                output.print(f"[dim]{entry.timestamp.strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
                extras = []
                if entry.repo:
                    extras.append(f"repo={entry.repo}")
                if entry.iteration is not None:
                    extras.append(f"iter={entry.iteration}")
                if entry.test_state:
                    extras.append(f"test={entry.test_state}")
                if entry.action:
                    extras.append(f"action={entry.action}")
                if extras:
                    output.print(f"  [dim]{'  '.join(extras)}[/dim]")
                output.print(f"  {entry.message}")
                if entry.open_question:
                    output.print(f"  [yellow]open:[/yellow] {entry.open_question}")
        else:
            output.json({
                "task": state.current_task,
                "entries": [
                    {
                        "timestamp": e.timestamp.isoformat(),
                        "message": e.message,
                        "repo": e.repo,
                        "iteration": e.iteration,
                        "test_state": e.test_state,
                        "action": e.action,
                        "open_question": e.open_question,
                    }
                    for e in entries
                ],
            })
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cli/test_log.py -v`
Expected: PASS.

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/log.py tests/cli/test_log.py
git commit -m "feat(log): structured flags (--action/--open/--test-state/--repo/--iteration/--show-open)"
```

---

## Task 6: Switch handoff prefers repo-tagged log entries

**Files:**
- Modify: `src/mship/core/switch.py` (`build_handoff`)
- Test: `tests/core/test_switch.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_switch.py`:
```python
def test_handoff_prefers_repo_tagged_log_entry(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace

    log_mgr = LogManager(workspace / ".mothership" / "logs")
    log_mgr.append("t", "generic older entry")
    log_mgr.append("t", "older shared entry", repo="shared")
    log_mgr.append("t", "most recent untagged")

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="shared")
    assert handoff.last_log_in_repo is not None
    assert handoff.last_log_in_repo.message == "older shared entry"


def test_handoff_falls_back_to_latest_when_no_repo_tag(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace

    log_mgr = LogManager(workspace / ".mothership" / "logs")
    log_mgr.append("t", "untagged only")

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="shared")
    assert handoff.last_log_in_repo is not None
    assert handoff.last_log_in_repo.message == "untagged only"
```

(The `LogManager`, `ConfigLoader`, and `ShellRunner` imports should already be present at the top of `tests/core/test_switch.py`; add any that are missing.)

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_switch.py -v -k "repo_tagged or no_repo_tag"`
Expected: FAIL — current behavior returns the latest entry regardless of repo.

- [ ] **Step 3: Update `build_handoff`**

In `src/mship/core/switch.py`, find the `# Last log` block (currently calls `log_manager.read(task.slug, last=1)`). Replace with:

```python
    # Last log — prefer entries tagged with this repo; fall back to latest overall.
    last_log = None
    try:
        entries = log_manager.read(task.slug)
        tagged = [e for e in entries if e.repo == repo]
        if tagged:
            last_log = tagged[-1]
        elif entries:
            last_log = entries[-1]
    except Exception:
        last_log = None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/core/test_switch.py -v`
Expected: PASS (all switch tests — new and existing).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/switch.py tests/core/test_switch.py
git commit -m "feat(switch): handoff prefers log entries tagged with the target repo"
```

---

## Task 7: Documentation

**Files:**
- Modify: `skills/working-with-mothership/SKILL.md`
- Modify: `README.md`

- [ ] **Step 1: Update the skill**

In `skills/working-with-mothership/SKILL.md`, under "During work", add below the existing `mship log` usage:
```
mship log "msg" --action "<what I'm doing>"     # structured: records action for session resume
mship log "msg" --open "<blocking question>"    # flag a blocker; re-read with mship log --show-open
mship log --show-open                            # list open questions for the current task
```

Under `mship test` examples, add:
```
mship test                                  # runs + shows diff vs previous iteration (new failure / fix / regression tags)
mship test --no-diff                        # skip the diff (plain pass/fail output)
```

Add a "What NOT to do" bullet:
```
- **Don't paste test output into `mship log`** — after every `mship test`, mship auto-logs a structured entry with iteration, test_state, and action. The iteration file under `.mothership/test-runs/` has stderr for failures.
```

- [ ] **Step 2: Update the README**

In `README.md`, in the CLI cheat sheet, replace the `mship test` lines:
```
mship test [--all] [--tag t] [--no-diff]  # runs in dep order; default shows diff vs previous iteration
```

And extend the `mship log` entries:
```
mship log                             # read task log
mship log "msg"                       # append breadcrumb (repo/iteration inferred from current state)
mship log "msg" --action X --open Y   # structured fields for cross-session recovery
mship log --show-open                 # list open questions for the current task
```

Also add a short paragraph to the state-safety narrative near the top (below the existing "Cross-repo context switches" paragraph):

```
**Iteration awareness.** Every `mship test` run gets a numbered iteration file with per-repo status, duration, exit code, and stderr tail. The next run shows the diff: new failures, fixes, regressions. Agents iterating on a test failure get a running log of what changed between attempts instead of re-reading stdout.
```

- [ ] **Step 3: Commit**

```bash
git add skills/working-with-mothership/SKILL.md README.md
git commit -m "docs: document test --diff and structured log flags"
```

---

## Self-Review

**Spec coverage:**
- Iteration file write + latest pointer — Task 3 (`write_run`).
- Retention (keep 20) — Task 3 (`prune`), Task 4 calls it.
- `compute_diff` taxonomy (first run / still passing / still failing / fix / new failure / regression) — Task 3 covered by unit tests.
- `Task.test_iteration` field — Task 2.
- `RepoResult.duration_ms` — Task 2.
- `mship test` flow (time, write, diff, log, render, exit) — Task 4.
- `--no-diff` — Task 4.
- Auto-appended structured log entry — Task 4.
- `LogEntry` structured fields + parser — Task 1.
- `LogManager.append` kwargs — Task 1.
- `mship log` flags + inference + `--show-open` — Task 5.
- Switch handoff repo-tag preference — Task 6.
- Docs — Task 7.
- Retention test — Task 3 (`test_prune_keeps_newest_n`).

**Placeholder scan:** none.

**Type consistency:**
- `write_run(state_dir, task_slug, iteration, started_at, duration_ms, results)` signature matches between Task 3 and Task 4 caller.
- `compute_diff(current, previous, pre_previous) -> dict` matches Task 3 impl and Task 4 caller.
- `LogEntry` keyword arguments (`repo`, `iteration`, `test_state`, `action`, `open_question`) match Task 1 definition, Task 4 auto-log call, Task 5 CLI call.
- `TestState = Literal["pass", "fail", "mixed"]` matches the runtime values emitted by Task 4.

**Known deferrals (explicit in spec):**
- Per-test-case parsing (pytest/go/jest adapters).
- `--timing` flag for slowdown detection.
- Query DSL for structured logs beyond `--show-open`.
