# Systematic Debugging Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `mship debug hypothesis/rule-out/resolved` as a thin sub-app that writes structured journal entries with research-aligned fields (`id`, `parent`, `evidence`, `category`); have `mship test` tag runs with `parent=<hypothesis-id>` when a debug thread is open; update the `systematic-debugging` skill to MANDATE these commands when mship is present.

**Architecture:** Extend `LogEntry` + `LogManager.append` with four optional kv fields (purely additive; no format version bump). Pure-function `current_debug_thread(log, slug)` in `core/debug.py` derives "open thread" from the journal at read time — no state-file mutation. New `cli/debug.py` registers three commands. One-line addition in `cli/exec.py` threads `parent` through. Skill doc update makes the coupling tight.

**Tech Stack:** Python 3.14, typer, pytest, stdlib `uuid`. No new runtime deps.

**Reference spec:** `docs/superpowers/specs/2026-04-23-systematic-debugging-loop-design.md`

---

## File structure

**New files:**
- `src/mship/core/debug.py` — `current_debug_thread(log, slug)` helper.
- `src/mship/cli/debug.py` — typer sub-app with three commands.
- `tests/core/test_debug.py` — unit tests for `current_debug_thread`.
- `tests/cli/test_debug.py` — integration tests for the sub-app.

**Modified files:**
- `src/mship/core/log.py` — add four optional fields to `LogEntry`; extend `_format_kv`, `_parse_kv`, and `LogManager.append`.
- `tests/core/test_log.py` — regression tests for the new kv fields.
- `src/mship/cli/__init__.py` — register the debug module.
- `src/mship/cli/exec.py` — `mship test` passes `parent` to `log_mgr.append` when debug thread is open.
- `tests/cli/test_exec.py` (or equivalent test file for `mship test`) — new test for parent-id on test run entries during open debug thread.
- `src/mship/skills/systematic-debugging/SKILL.md` — tight-coupling section mandating mship commands when present.

**Task ordering rationale:** Task 1 (log schema) is foundational — every later task writes these kv fields. Task 2 (`current_debug_thread`) depends on the schema. Task 3 (cli commands) uses both. Task 4 (mship test integration) uses Task 2. Task 5 (skill doc) is pure-docs. Task 6 is smoke + PR.

---

## Task 1: Extend LogEntry schema with debug-vocabulary fields

**Files:**
- Modify: `src/mship/core/log.py`
- Modify: `tests/core/test_log.py`

**Context:** Add four optional fields to `LogEntry`: `id`, `parent`, `evidence`, `category`. Extend `_format_kv` to emit them, `_parse_kv` roundtrip happens via generic match (already handles arbitrary kv), `LogManager.append` takes matching kwargs. Purely additive — no existing caller breaks.

- [ ] **Step 1.1: Write failing tests**

Append to `tests/core/test_log.py`:

```python
def test_append_writes_new_kv_fields(tmp_path: Path):
    """id/parent/evidence/category are stored as kv on the journal line."""
    from mship.core.log import LogManager
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    mgr.append(
        "t", "test hypothesis",
        action="hypothesis",
        id="a3f4c2e1",
        evidence="test-runs/5",
    )
    content = (tmp_path / "logs" / "t.md").read_text()
    assert "id=a3f4c2e1" in content
    assert "evidence=" in content and "test-runs/5" in content


def test_read_parses_new_kv_fields(tmp_path: Path):
    from mship.core.log import LogManager
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    mgr.append(
        "t", "refuted because TZ is fixed",
        action="ruled-out",
        id="b7d9e2a0",
        parent="a3f4c2e1",
        evidence="test-runs/6",
        category="tool-output-misread",
    )
    entries = mgr.read("t")
    assert len(entries) == 1
    e = entries[0]
    assert e.id == "b7d9e2a0"
    assert e.parent == "a3f4c2e1"
    assert e.evidence == "test-runs/6"
    assert e.category == "tool-output-misread"


def test_append_backcompat_no_new_kv(tmp_path: Path):
    """Existing callers (no new kwargs) still produce identical output."""
    from mship.core.log import LogManager
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    mgr.append("t", "plain message", action="committed")
    content = (tmp_path / "logs" / "t.md").read_text()
    # No new kv fields appear when not specified.
    assert "id=" not in content
    assert "parent=" not in content
    assert "evidence=" not in content
    assert "category=" not in content
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_log.py -v -k "new_kv or backcompat"`
Expected: the first two fail (`LogEntry` has no `id`/`parent`/etc. attrs); the backcompat test likely passes already.

- [ ] **Step 1.3: Extend `LogEntry` + `_format_kv` + `LogManager.append`**

Edit `src/mship/core/log.py`. Replace the `LogEntry` dataclass:

```python
@dataclass
class LogEntry:
    timestamp: datetime
    message: str
    repo: Optional[str] = None
    iteration: Optional[int] = None
    test_state: Optional[TestState] = None
    action: Optional[str] = None
    open_question: Optional[str] = None
    id: Optional[str] = None
    parent: Optional[str] = None
    evidence: Optional[str] = None
    category: Optional[str] = None
```

Extend `_format_kv` — append new emitters at the end, in this order (after `open=`):

```python
def _format_kv(entry: LogEntry) -> str:
    parts: list[str] = []
    if entry.repo is not None:
        parts.append(f"repo={entry.repo}")
    if entry.iteration is not None:
        parts.append(f"iter={entry.iteration}")
    if entry.test_state is not None:
        parts.append(f"test={entry.test_state}")
    if entry.action is not None:
        if ' ' in entry.action:
            a = entry.action.replace('"', '\\"')
            parts.append(f'action="{a}"')
        else:
            parts.append(f"action={entry.action}")
    if entry.open_question is not None:
        q = entry.open_question.replace('"', '\\"')
        parts.append(f'open="{q}"')
    if entry.id is not None:
        parts.append(f"id={entry.id}")
    if entry.parent is not None:
        parts.append(f"parent={entry.parent}")
    if entry.evidence is not None:
        # Evidence may contain spaces, colons, slashes — always quote.
        ev = entry.evidence.replace('"', '\\"')
        parts.append(f'evidence="{ev}"')
    if entry.category is not None:
        if ' ' in entry.category:
            c = entry.category.replace('"', '\\"')
            parts.append(f'category="{c}"')
        else:
            parts.append(f"category={entry.category}")
    return "  " + "  ".join(parts) if parts else ""
```

Extend `LogManager.append` signature + body:

```python
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
        id: Optional[str] = None,
        parent: Optional[str] = None,
        evidence: Optional[str] = None,
        category: Optional[str] = None,
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
            id=id,
            parent=parent,
            evidence=evidence,
            category=category,
        )
        kv = _format_kv(entry)
        with open(path, "a") as f:
            f.write(f"\n## {timestamp}{kv}\n{message}\n")
```

Extend `_parse` to populate the new fields from `kv`:

```python
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
                id=kv.get("id"),
                parent=kv.get("parent"),
                evidence=kv.get("evidence"),
                category=kv.get("category"),
            ))
        return entries
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_log.py -v`
Expected: all pass (3 new + existing regression).

- [ ] **Step 1.5: Run broader `tests/core/` for regressions**

Run: `uv run pytest tests/core/ --ignore=tests/core/view/test_web_port.py -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 1.6: Commit**

```bash
git add src/mship/core/log.py tests/core/test_log.py
git commit -m "feat(log): add id/parent/evidence/category kv fields to LogEntry"
mship journal "#30: LogEntry gains id/parent/evidence/category optional kv fields for debug-vocabulary alignment with CodeTracer/AgentStepper tree compilers" --action committed
```

---

## Task 2: `current_debug_thread` helper

**Files:**
- Create: `src/mship/core/debug.py`
- Create: `tests/core/test_debug.py`

**Context:** Pure function over a journal. "Open thread" = sequence starting at the FIRST `action=hypothesis` entry after the most recent `action=debug-resolved` (or from task start if never resolved), continuing to the end. Returns the list of entries constituting that open thread, or None when no thread is open.

- [ ] **Step 2.1: Write failing tests**

Create `tests/core/test_debug.py`:

```python
"""Tests for current_debug_thread. See #30."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.log import LogManager


def _at(mgr: LogManager, slug: str, **kwargs) -> None:
    """Append helper."""
    mgr.append(slug, kwargs.pop("msg", "x"), **kwargs)


def test_empty_journal_returns_none(tmp_path: Path):
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    assert current_debug_thread(mgr, "t") is None


def test_journal_with_no_hypotheses_returns_none(tmp_path: Path):
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="did a commit", action="committed")
    _at(mgr, "t", msg="ran tests", action="ran tests", iteration=1, test_state="pass")
    assert current_debug_thread(mgr, "t") is None


def test_single_open_hypothesis_returns_one_entry(tmp_path: Path):
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="flaky assertion", action="hypothesis", id="h1")
    thread = current_debug_thread(mgr, "t")
    assert thread is not None
    assert len(thread) == 1
    assert thread[0].action == "hypothesis"
    assert thread[0].id == "h1"


def test_hypothesis_plus_ruled_out_still_open(tmp_path: Path):
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="H1", action="hypothesis", id="h1")
    _at(mgr, "t", msg="R1", action="ruled-out", id="r1", parent="h1")
    thread = current_debug_thread(mgr, "t")
    assert thread is not None
    assert len(thread) == 2


def test_closed_thread_returns_none(tmp_path: Path):
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="H1", action="hypothesis", id="h1")
    _at(mgr, "t", msg="R1", action="ruled-out", id="r1", parent="h1")
    _at(mgr, "t", msg="done", action="debug-resolved", id="res1")
    assert current_debug_thread(mgr, "t") is None


def test_reopened_thread_returns_only_new_segment(tmp_path: Path):
    """Close + reopen: return only the new-segment entries."""
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="H1", action="hypothesis", id="h1")
    _at(mgr, "t", msg="done1", action="debug-resolved", id="res1")
    _at(mgr, "t", msg="H2", action="hypothesis", id="h2")
    _at(mgr, "t", msg="R2", action="ruled-out", id="r2", parent="h2")
    thread = current_debug_thread(mgr, "t")
    assert thread is not None
    ids = [e.id for e in thread]
    assert ids == ["h2", "r2"]


def test_resolved_without_prior_hypothesis_returns_none(tmp_path: Path):
    """A resolved entry with no hypothesis before it doesn't constitute a thread."""
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="weird", action="debug-resolved", id="res1")
    assert current_debug_thread(mgr, "t") is None


def test_interleaved_non_debug_entries_included(tmp_path: Path):
    """Test runs, commits, etc. during an open thread are part of the thread."""
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="H1", action="hypothesis", id="h1")
    _at(mgr, "t", msg="iter 3: 2/3", action="ran tests", iteration=3, test_state="mixed", parent="h1")
    _at(mgr, "t", msg="code change", action="committed")
    thread = current_debug_thread(mgr, "t")
    assert thread is not None
    assert len(thread) == 3
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_debug.py -v`
Expected: all fail — `ModuleNotFoundError: No module named 'mship.core.debug'`.

- [ ] **Step 2.3: Create the module**

Write `src/mship/core/debug.py`:

```python
"""Debug-thread derivation from the per-task journal.

"Open thread" is computed purely from journal content — no state-file
mutation. The thread is the sequence of entries from the FIRST `action=hypothesis`
after the most recent `action=debug-resolved` (or from task start if never
resolved), continuing to the end of the journal. Returns None when there is no
open thread. See #30.
"""
from __future__ import annotations

from mship.core.log import LogEntry, LogManager


def current_debug_thread(log: LogManager, slug: str) -> list[LogEntry] | None:
    """Return the list of entries in the current open debug thread, or None.

    The thread opens at the first `hypothesis` action after the most recent
    `debug-resolved` (or from task start if there has been no resolution).
    It remains open until the end of the journal or the next `debug-resolved`.
    """
    entries = log.read(slug)
    if not entries:
        return None

    # Find the index of the most recent `debug-resolved`. Everything before
    # or at that index is closed.
    last_resolved_idx = -1
    for i, e in enumerate(entries):
        if e.action == "debug-resolved":
            last_resolved_idx = i

    # Search for the first `hypothesis` entry AFTER that boundary.
    first_hypothesis_idx = None
    for i in range(last_resolved_idx + 1, len(entries)):
        if entries[i].action == "hypothesis":
            first_hypothesis_idx = i
            break
    if first_hypothesis_idx is None:
        return None

    return entries[first_hypothesis_idx:]
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_debug.py -v`
Expected: 8 passed.

- [ ] **Step 2.5: Commit**

```bash
git add src/mship/core/debug.py tests/core/test_debug.py
git commit -m "feat(core): current_debug_thread derives open thread from journal"
mship journal "#30: current_debug_thread is a pure function over journal entries — no state-file mutation; open thread = first hypothesis after most recent debug-resolved, through end of journal" --action committed
```

---

## Task 3: `mship debug` sub-app

**Files:**
- Create: `src/mship/cli/debug.py`
- Create: `tests/cli/test_debug.py`
- Modify: `src/mship/cli/__init__.py` — register the module.

**Context:** Three thin commands that write journal entries via `LogManager.append`. Auto-generate 8-char UUID prefix for `id` when user doesn't supply `--id`. Advisory stderr warning when `resolved` is called with no prior hypothesis in the journal. No guardrails on hypothesis/rule-out (journal is source of truth; derived at read time).

- [ ] **Step 3.1: Write failing integration tests**

Create `tests/cli/test_debug.py`:

```python
"""Integration tests for `mship debug` sub-app. See #30."""
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def test_debug_hypothesis_writes_journal_entry(configured_git_app: Path):
    runner.invoke(app, ["spawn", "hypo test", "--repos", "shared", "--skip-setup"])
    result = runner.invoke(
        app, ["debug", "hypothesis", "test is flaky",
              "--evidence", "test-runs/5", "--task", "hypo-test"],
    )
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "hypo-test.md").read_text()
    assert "action=hypothesis" in log
    assert "test is flaky" in log
    assert "id=" in log  # auto-generated
    assert "evidence=" in log and "test-runs/5" in log


def test_debug_hypothesis_honors_explicit_id(configured_git_app: Path):
    runner.invoke(app, ["spawn", "id test", "--repos", "shared", "--skip-setup"])
    result = runner.invoke(
        app, ["debug", "hypothesis", "H1", "--id", "h1", "--task", "id-test"],
    )
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "id-test.md").read_text()
    assert "id=h1" in log


def test_debug_rule_out_writes_parent_kv(configured_git_app: Path):
    runner.invoke(app, ["spawn", "ro test", "--repos", "shared", "--skip-setup"])
    runner.invoke(
        app, ["debug", "hypothesis", "H1", "--id", "h1", "--task", "ro-test"],
    )
    result = runner.invoke(
        app, ["debug", "rule-out", "not it", "--parent", "h1", "--task", "ro-test"],
    )
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "ro-test.md").read_text()
    assert "action=ruled-out" in log
    assert "parent=h1" in log


def test_debug_rule_out_with_category(configured_git_app: Path):
    runner.invoke(app, ["spawn", "cat test", "--repos", "shared", "--skip-setup"])
    runner.invoke(app, ["debug", "hypothesis", "H", "--id", "h", "--task", "cat-test"])
    result = runner.invoke(
        app, ["debug", "rule-out", "R", "--parent", "h",
              "--category", "tool-output-misread", "--task", "cat-test"],
    )
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "cat-test.md").read_text()
    assert "category=tool-output-misread" in log


def test_debug_resolved_writes_entry(configured_git_app: Path):
    runner.invoke(app, ["spawn", "res test", "--repos", "shared", "--skip-setup"])
    runner.invoke(app, ["debug", "hypothesis", "H", "--id", "h", "--task", "res-test"])
    result = runner.invoke(
        app, ["debug", "resolved", "fixed by commit abc", "--task", "res-test"],
    )
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "res-test.md").read_text()
    assert "action=debug-resolved" in log
    assert "fixed by commit abc" in log


def test_debug_resolved_without_hypothesis_warns(configured_git_app: Path):
    """Advisory stderr warning when closing without any prior hypothesis."""
    runner.invoke(app, ["spawn", "warn test", "--repos", "shared", "--skip-setup"])
    result = runner.invoke(
        app, ["debug", "resolved", "no thread", "--task", "warn-test"],
    )
    # Entry still written, exit 0.
    assert result.exit_code == 0, result.output
    # Warning surfaced.
    assert "warning" in (result.output or "").lower()
    assert "no prior hypothesis" in (result.output or "").lower() or "without any prior hypothesis" in (result.output or "").lower()
    log = (configured_git_app / ".mothership" / "logs" / "warn-test.md").read_text()
    assert "action=debug-resolved" in log


def test_debug_auto_id_is_8_char_hex(configured_git_app: Path):
    """Auto-generated id is 8 lowercase-hex chars."""
    import re as _re
    runner.invoke(app, ["spawn", "auto id", "--repos", "shared", "--skip-setup"])
    runner.invoke(app, ["debug", "hypothesis", "H", "--task", "auto-id"])
    log = (configured_git_app / ".mothership" / "logs" / "auto-id.md").read_text()
    m = _re.search(r"id=([a-f0-9]+)", log)
    assert m is not None
    assert len(m.group(1)) == 8
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_debug.py -v`
Expected: all fail — `No such command 'debug'`.

- [ ] **Step 3.3: Create the module**

Write `src/mship/cli/debug.py`:

```python
"""`mship debug` sub-app — structured journal entries for debugging. See #30.

Three commands: `hypothesis`, `rule-out`, `resolved`. Each writes a single
journal entry via `LogManager.append`. Auto-generates an 8-char hex id when
the user doesn't provide `--id`. Advisory stderr warning on `resolved` without
any prior hypothesis in the journal.
"""
import uuid
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


def _auto_id() -> str:
    """Short UUID prefix (8 hex chars). Collision odds are fine for per-task volumes."""
    return uuid.uuid4().hex[:8]


def register(app: typer.Typer, get_container):
    debug_app = typer.Typer(help="Structured journal entries for debugging. See #30.")

    @debug_app.command()
    def hypothesis(
        text: str = typer.Argument(..., help="Hypothesis statement"),
        evidence: Optional[str] = typer.Option(
            None, "--evidence",
            help="Free-form evidence ref (e.g. test-runs/5, HEAD, path:12-18)",
        ),
        id_: Optional[str] = typer.Option(
            None, "--id",
            help="Human-readable handle (default: auto 8-char hex)",
        ),
        task: Optional[str] = typer.Option(
            None, "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env.",
        ),
    ):
        """Log a debugging hypothesis."""
        container = get_container()
        output = Output()
        state = container.state_manager().load()
        resolved = resolve_for_command("debug", state, task, output)
        entry_id = id_ if id_ else _auto_id()
        container.log_manager().append(
            resolved.task.slug, text,
            action="hypothesis",
            id=entry_id,
            evidence=evidence,
        )

    @debug_app.command(name="rule-out")
    def rule_out(
        text: str = typer.Argument(..., help="Why the hypothesis is ruled out"),
        parent: Optional[str] = typer.Option(
            None, "--parent", help="id of the hypothesis being refuted",
        ),
        evidence: Optional[str] = typer.Option(
            None, "--evidence", help="Evidence ref refuting the hypothesis",
        ),
        category: Optional[str] = typer.Option(
            None, "--category",
            help="Optional classification (e.g. 'tool-output-misread')",
        ),
        id_: Optional[str] = typer.Option(
            None, "--id", help="Handle for this rule-out entry",
        ),
        task: Optional[str] = typer.Option(None, "--task"),
    ):
        """Log a ruled-out hypothesis."""
        container = get_container()
        output = Output()
        state = container.state_manager().load()
        resolved = resolve_for_command("debug", state, task, output)
        entry_id = id_ if id_ else _auto_id()
        container.log_manager().append(
            resolved.task.slug, text,
            action="ruled-out",
            id=entry_id,
            parent=parent,
            evidence=evidence,
            category=category,
        )

    @debug_app.command()
    def resolved(
        text: str = typer.Argument(..., help="Root cause + fix summary"),
        id_: Optional[str] = typer.Option(None, "--id"),
        task: Optional[str] = typer.Option(None, "--task"),
    ):
        """Close the open debug thread."""
        container = get_container()
        output = Output()
        state = container.state_manager().load()
        resolved_task = resolve_for_command("debug", state, task, output)

        # Advisory: warn if no prior hypothesis entry exists since the most
        # recent debug-resolved (or ever). Journal write succeeds regardless.
        log = container.log_manager()
        entries = log.read(resolved_task.task.slug)
        last_resolved_idx = -1
        for i, e in enumerate(entries):
            if e.action == "debug-resolved":
                last_resolved_idx = i
        has_hypothesis_in_segment = any(
            e.action == "hypothesis"
            for e in entries[last_resolved_idx + 1 :]
        )
        if not has_hypothesis_in_segment:
            output.warning(
                "logging debug-resolved without any prior hypothesis entries in the current segment"
            )

        entry_id = id_ if id_ else _auto_id()
        log.append(
            resolved_task.task.slug, text,
            action="debug-resolved",
            id=entry_id,
        )

    app.add_typer(debug_app, name="debug")
```

- [ ] **Step 3.4: Register the sub-app**

Edit `src/mship/cli/__init__.py`. Add near the other imports (keep grouping):

```python
from mship.cli import debug as _debug_mod
```

Add near the other register calls:

```python
_debug_mod.register(app, get_container)
```

- [ ] **Step 3.5: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_debug.py -v`
Expected: 7 passed.

- [ ] **Step 3.6: Run broader `tests/cli/` for regressions**

Run: `uv run pytest tests/cli/ -q 2>&1 | tail -3`
Expected: all pass. (Sub-app registration is purely additive; no existing command affected.)

- [ ] **Step 3.7: Commit**

```bash
git add src/mship/cli/debug.py src/mship/cli/__init__.py tests/cli/test_debug.py
git commit -m "feat(cli): mship debug sub-app with hypothesis/rule-out/resolved"
mship journal "#30: mship debug {hypothesis,rule-out,resolved} commands write structured journal entries; auto-UUID id; advisory warn on resolved-without-hypothesis" --action committed
```

---

## Task 4: `mship test` parent-id integration

**Files:**
- Modify: `src/mship/cli/exec.py` — pass `parent=<latest-hypothesis-id>` to `log_mgr.append` for `mship test` entries during open debug thread.
- Modify: `tests/cli/test_exec.py` (or wherever `mship test` integration tests live) — new test.

**Context:** Currently `mship test` writes a journal entry with `action="ran tests"`. During an open debug thread, enrich that entry with `parent=<latest-unresolved-hypothesis-id>`. Use `current_debug_thread` from Task 2 to find the latest hypothesis id.

- [ ] **Step 4.1: Write failing test**

First, find where `mship test`'s journal append lives. Run: `grep -n 'action="ran tests"' src/mship/cli/exec.py`. It's a single call site.

Check which test file covers `mship test`. Run: `grep -rln "test_cmd\|mship test\|ran tests" tests/` to locate.

Append to the most logical `tests/cli/test_exec.py` (or equivalent). If none exists, create it:

```python
def test_test_run_journal_entry_includes_parent_during_open_debug_thread(configured_git_app: Path):
    """mship test during open debug thread enriches the `ran tests` journal
    entry with parent=<latest-hypothesis-id>. See #30."""
    from unittest.mock import MagicMock
    from mship.cli import container as cli_container
    from mship.util.shell import ShellResult, ShellRunner

    runner.invoke(app, ["spawn", "test parent", "--repos", "shared", "--skip-setup"])
    # Open a debug thread.
    runner.invoke(
        app, ["debug", "hypothesis", "H1", "--id", "h1", "--task", "test-parent"],
    )

    # Mock the test executor so `mship test` doesn't actually run tests.
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    cli_container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["test", "--task", "test-parent"])
        assert result.exit_code == 0, result.output
        log = (configured_git_app / ".mothership" / "logs" / "test-parent.md").read_text()
        assert "action=\"ran tests\"" in log or "action=ran tests" in log
        # The ran-tests entry must carry parent=h1.
        assert "parent=h1" in log
    finally:
        cli_container.shell.reset_override()


def test_test_run_journal_entry_no_parent_when_no_debug_thread(configured_git_app: Path):
    """Regression: without open debug thread, ran-tests entry has no parent kv."""
    from unittest.mock import MagicMock
    from mship.cli import container as cli_container
    from mship.util.shell import ShellResult, ShellRunner

    runner.invoke(app, ["spawn", "plain test", "--repos", "shared", "--skip-setup"])
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    cli_container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["test", "--task", "plain-test"])
        assert result.exit_code == 0, result.output
        log = (configured_git_app / ".mothership" / "logs" / "plain-test.md").read_text()
        assert "parent=" not in log
    finally:
        cli_container.shell.reset_override()
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_exec.py -v -k "parent"` (or whichever file you placed the test in)
Expected: the `includes_parent` test fails — `parent=h1` not in log.

- [ ] **Step 4.3: Wire in the `parent` kv**

Edit `src/mship/cli/exec.py`. Find the `container.log_manager().append(` call with `action="ran tests"`. Currently:

```python
        container.log_manager().append(
            t.slug,
            f"iter {new_iter}: {pass_count}/{total} passing",
            iteration=new_iter,
            test_state=test_state,
            action="ran tests",
        )
```

Replace with:

```python
        # If a debug thread is open, attach parent=<latest hypothesis id> so
        # tree-compilation tools can fold this test run into the hypothesis
        # being evaluated. See #30.
        from mship.core.debug import current_debug_thread
        thread = current_debug_thread(container.log_manager(), t.slug)
        parent_id = None
        if thread:
            # Latest `hypothesis` entry in the thread (search from end).
            for e in reversed(thread):
                if e.action == "hypothesis":
                    parent_id = e.id
                    break

        container.log_manager().append(
            t.slug,
            f"iter {new_iter}: {pass_count}/{total} passing",
            iteration=new_iter,
            test_state=test_state,
            action="ran tests",
            parent=parent_id,
        )
```

- [ ] **Step 4.4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_exec.py -v -k "parent"`
Expected: 2 passed.

- [ ] **Step 4.5: Run broader regression check**

Run: `uv run pytest tests/ --ignore=tests/core/view/test_web_port.py -q 2>&1 | tail -3`
Expected: all pass. Existing `mship test` tests aren't affected — they'll have `parent_id=None` which isn't emitted as kv.

- [ ] **Step 4.6: Commit**

```bash
git add src/mship/cli/exec.py tests/cli/test_exec.py
git commit -m "feat(test): attach parent=<hypothesis-id> to ran-tests entries in debug threads"
mship journal "#30: mship test enriches its ran-tests journal entry with parent=<latest-hypothesis-id> when debug thread is open; tree compilers fold test runs into hypotheses" --action committed
```

---

## Task 5: Skill doc tight coupling

**Files:**
- Modify: `src/mship/skills/systematic-debugging/SKILL.md`

**Context:** Add a section mandating `mship debug` invocation when mship is present. Graceful degradation: if mship is absent, fall back to the skill's prior free-form methodology.

- [ ] **Step 5.1: Read the existing skill doc**

Run: `cat src/mship/skills/systematic-debugging/SKILL.md | head -80`

Identify the methodology section (likely headed "Methodology" or "The Process" or similar) and the natural insertion point — typically right after the core methodology steps are described, before troubleshooting / examples.

- [ ] **Step 5.2: Add the mship-integration section**

Edit `src/mship/skills/systematic-debugging/SKILL.md`. Add this section (as a new `##` heading) right after the main methodology description:

```markdown
## mship integration (REQUIRED when mship is present)

If `mship` is available in PATH and the current working directory is inside an mship workspace, you MUST invoke the tool at each methodology checkpoint. This generates the durable audit trace the supervisor relies on and enables tree-compilation tools to reconstruct your debugging path.

- **When forming a hypothesis:**
  ```
  mship debug hypothesis "<claim>" --evidence <ref> [--id <slug>]
  ```
  `<ref>` is a free-form pointer like `test-runs/5`, `HEAD`, or `<path>:<start>-<end>`. `--id` is optional (mship auto-generates an 8-char hex handle); use it when you want human-readable references (e.g. `--id h1`).

- **When ruling out a hypothesis:**
  ```
  mship debug rule-out "<reason>" --parent <hypothesis-id> --evidence <ref> [--category <label>]
  ```
  `--parent` points at the hypothesis id you are refuting. `--category` is optional; adopt the [AgentRx](https://arxiv.org) failure taxonomy (e.g. `intent-plan-misalignment`, `tool-output-misread`, `invention-of-new-information`) if doing cross-session analysis.

- **When closing the investigation:**
  ```
  mship debug resolved "<root cause and fix summary>"
  ```
  Only an explicit `debug-resolved` closes the thread. A passing test run does NOT implicitly close it — write the explicit entry so the supervisor can audit what you concluded.

- **While running tests:** `mship test` during an open debug thread automatically enriches its journal entry with `parent=<latest-hypothesis-id>`. Nothing extra for you to do.

### If mship is NOT available

Non-mship projects, mship not on PATH, or running outside a workspace: fall back to the methodology as described in the rest of this skill. Log hypotheses and rule-outs as inline notes, commit messages, or PR comments — whatever durable medium is available. The structure (hypothesis → evidence → rule-out → resolution) stays the same; the storage differs.

### Why tight coupling here

Research from 2026 agentic-coding literature (Debug-gym, AgentRx, SWE-agent) is unambiguous: for debugging specifically, loosely coupled tools get skipped under context pressure, and the diagnostic trail vanishes. Mandating invocation at each step produces a verifiable BDI (Belief-Desire-Intention) trace the supervisor can reconstruct — which is the whole point of doing systematic debugging in the first place.
```

- [ ] **Step 5.3: Commit**

```bash
git add src/mship/skills/systematic-debugging/SKILL.md
git commit -m "docs(skill): tight-couple systematic-debugging methodology to mship debug commands"
mship journal "#30: systematic-debugging skill now mandates mship debug <verb> invocation at each methodology checkpoint when mship is present; falls back gracefully otherwise" --action committed
```

---

## Task 6: Smoke + PR

**Files:**
- None (verification + PR only).

- [ ] **Step 6.1: Reinstall**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/debug-loop
uv tool install --reinstall --from . mothership
```

- [ ] **Step 6.2: Smoke the full debug flow**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/debug-loop

# Log a hypothesis.
mship debug hypothesis "flaky test is timezone-dependent" --evidence "test-runs/1" --id h1
# Rule it out.
mship debug rule-out "TZ is set in fixture; still fails" --parent h1 --evidence "test-runs/2" --category "tool-output-misread"
# Close the thread.
mship debug resolved "root cause was cache eviction race; fix in HEAD"

# Inspect the journal.
cat .mothership/logs/debug-loop.md | tail -20
```

Expected: three journal entries with the correct `action=`, `id=`, `parent=`, `evidence=`, `category=` kvs and messages.

- [ ] **Step 6.3: Smoke the advisory warning**

```bash
mship debug resolved "no thread to close" 2>&1 | grep -i warning
```

Expected: warning text containing "no prior hypothesis" (or similar).

- [ ] **Step 6.4: Smoke the `mship test` parent integration**

`mship test` in this repo would run the full suite — too slow for a smoke. The unit + integration tests (Task 4) cover the behavior. Skip this step unless you've got time to wait.

- [ ] **Step 6.5: Full pytest**

```bash
uv run pytest tests/ --ignore=tests/core/view/test_web_port.py 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 6.6: Open the PR**

Write `/tmp/debug-loop-body.md`:

```markdown
## Summary

Closes #30.

Adds `mship debug hypothesis/rule-out/resolved` as structured journal commands, with `mship test` auto-attaching `parent=<hypothesis-id>` to test runs during an open debug thread. Updates the `systematic-debugging` skill to mandate these commands when mship is present.

### Research alignment (2026)

- **Explicit closure only** (SWE-agent, AgentRx): `debug-resolved` is the sole signal; no implicit close on passing tests.
- **UUID-based IDs** (NVIDIA NeMo): auto-generated 8-char hex prefix per entry; `--id <slug>` for human-readable override.
- **Evidence refs** (Agent Trace v0.1.0): free-form `--evidence <ref>`; convention for `<path>:<start>-<end>` documented in the skill.
- **Verification bundled into triggering action** (CodeTracer): `mship test` enriches its existing `ran tests` entry with `parent=<id>` — ONE entry, not two.
- **Optional category taxonomy** (AgentRx): `--category <label>` on rule-out for cross-session classification.
- **Tight skill-tool coupling for debugging**: skill MANDATES commands when mship is present; falls back without.

### Design rules

- Journal is source of truth. No state-file mutation.
- Advisory stderr warning (not a block) on `debug-resolved` without any prior `hypothesis`.
- Free-form `evidence`, `category`, `--id`, `--parent` — mship doesn't validate semantics.
- `current_debug_thread` is a pure function over journal entries.

## Test plan

- [x] `tests/core/test_log.py`: 3 new tests for the four new kv fields.
- [x] `tests/core/test_debug.py`: 8 unit tests for `current_debug_thread` across thread-open/closed states.
- [x] `tests/cli/test_debug.py`: 7 integration tests for the sub-app (hypothesis, rule-out, resolved, --id, --parent, --category, advisory warning, auto-id format).
- [x] `tests/cli/test_exec.py`: 2 new tests — parent kv present during open thread; absent otherwise.
- [x] Full suite green.
- [x] Manual smoke: full hypothesis → rule-out → resolved sequence produces the expected journal entries; resolved-without-hypothesis emits the advisory warning.

## Anti-goals preserved

- No state-file mutation.
- No hard blocks.
- No `debug show` / `debug status` command in v1.
- No loop-detection / semantic-similarity analysis (supervisor-layer concern).
- No tree-reconstruction inside mship — downstream tools consume `id`/`parent` kvs.
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/debug-loop
mship finish --body-file /tmp/debug-loop-body.md --title "feat(mship): systematic debugging loop — debug commands + test integration (#30)"
```

Expected: PR URL returned.

---

## Done when

- [x] `LogEntry` + `LogManager.append` + `_format_kv`/`_parse_kv` support `id`/`parent`/`evidence`/`category`.
- [x] `current_debug_thread` correctly derives the open thread from journal entries.
- [x] `mship debug hypothesis/rule-out/resolved` commands work and write the expected entries.
- [x] `mship test` attaches `parent=<hypothesis-id>` to its journal entry during an open debug thread.
- [x] `systematic-debugging` skill mandates `mship debug` invocation when present; falls back otherwise.
- [x] 20+ new tests pass (3 log + 8 debug-core + 7 cli-debug + 2 test-exec).
- [x] Full pytest green.
- [x] Manual smoke confirms the full flow + advisory warning.
