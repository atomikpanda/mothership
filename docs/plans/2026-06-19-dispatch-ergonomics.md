# Dispatch Ergonomics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `dispatch-ergonomics` (approved) — `specs/2026-06-19-dispatch-ergonomics.md`. Closes MOS-185, MOS-143 (dup of 185), MOS-142, MOS-181.

**Goal:** Make the implementation plan the single source of per-subagent instructions and make `mship dispatch` / `mship test` the path of least resistance, so plan execution (including unattended) is deterministic and carries test evidence — without ever spawning duplicate tasks.

**Architecture:** Four independent units. (1) A pure `extract_plan_task` helper in `core/dispatch.py` pulls an anchored task block out of plan text. (2) `cli/dispatch.py` gains `--plan`/`--plan-task` and an exactly-one-of instruction-source rule, feeding the resolved instruction into the unchanged prompt builder. (3) `core/spec_dispatch.py` + `cli/spec.py` gain explicit `--task` binding plus idempotent reuse. (4) The bundled `writing-plans` and `subagent-driven-development` skills adopt the anchors and the `mship dispatch --plan-task` / `mship test` commands, guarded by tests.

**Tech Stack:** Python 3.14, uv, pytest, Typer, Pydantic. Run tests in the worktree with `mship test` (records the evidence `mship finish --require-tests` checks). For a single test during TDD, `uv run --no-sync pytest <path>::<name> -q`.

---

## The anchor format (used throughout)

A task block in a plan is delimited by HTML-comment anchors so `extract_plan_task` can pull it out without parsing markdown headings:

```markdown
<!-- mship:task id=1 -->
### Task 1: …
… full task text …
<!-- /mship:task -->
```

`id=<value>` is matched exactly. The tasks in *this* plan are wrapped in these anchors as the canonical example.

## File Structure

- `src/mship/core/dispatch.py` — **modify**: add the pure `extract_plan_task(plan_text, task_id) -> str` next to the existing prompt builder. No I/O.
- `src/mship/cli/dispatch.py` — **modify**: make `--instruction` optional; add `--plan`/`--plan-task`; enforce exactly-one-of instruction source (inline / stdin `-` / `--plan-task`); read the plan and call `extract_plan_task`.
- `src/mship/core/spec_dispatch.py` — **modify**: `dispatch_spec` gains a `task_slug` param (explicit `--task` binding), idempotent reuse keyed on `spec.task_slug`, and accepts an already-`dispatched` spec.
- `src/mship/cli/spec.py` — **modify**: the `spec dispatch` command gains a `--task` option threaded into `dispatch_spec`.
- `src/mship/skills/writing-plans/SKILL.md` — **modify**: wrap the task template in anchors; document `mship dispatch --task <slug> --plan <plan> --plan-task <N>` + a `mship test` step.
- `src/mship/skills/subagent-driven-development/SKILL.md` — **modify**: build implementer prompts via `mship dispatch … --plan-task`; subagents run `mship test`.
- `src/mship/skills/subagent-driven-development/implementer-prompt.md` — **modify**: verification step says run `mship test` (not bare pytest) in a mothership workspace.
- `tests/core/test_dispatch.py` — **create**: `extract_plan_task` table.
- `tests/cli/test_dispatch.py` — **modify**: `--plan-task`, exactly-one-of, stdin, inline-still-works.
- `tests/core/test_spec_dispatch.py` — **modify**: `--task` binding, idempotent reuse, unknown/ambiguous errors.
- `tests/skills/test_skill_dispatch_ergonomics.py` — **create**: guard tests on bundled skill text.

---

<!-- mship:task id=1 -->
### Task 1: `extract_plan_task` pure helper

**Files:**
- Modify: `src/mship/core/dispatch.py` (add helper + `import re` near the top)
- Test: `tests/core/test_dispatch.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_dispatch.py`:

```python
"""Tests for pure helpers in mship.core.dispatch."""
from __future__ import annotations

import pytest

from mship.core.dispatch import extract_plan_task


PLAN = """\
# Some plan

intro text

<!-- mship:task id=1 -->
### Task 1: first

do the first thing
<!-- /mship:task -->

middle text

<!-- mship:task id=2 -->
### Task 2: second

do the second thing
<!-- /mship:task -->
"""


def test_extract_returns_inner_block():
    out = extract_plan_task(PLAN, "1")
    assert "### Task 1: first" in out
    assert "do the first thing" in out
    # does not leak the other block
    assert "second thing" not in out
    # anchors themselves are stripped out
    assert "mship:task" not in out


def test_extract_picks_the_right_block_in_a_multi_task_plan():
    out = extract_plan_task(PLAN, "2")
    assert "### Task 2: second" in out
    assert "do the second thing" in out
    assert "first thing" not in out


def test_extract_missing_id_raises():
    with pytest.raises(ValueError, match="no task with id '99'"):
        extract_plan_task(PLAN, "99")


def test_extract_duplicate_id_raises():
    dup = (
        "<!-- mship:task id=1 -->\nA\n<!-- /mship:task -->\n"
        "<!-- mship:task id=1 -->\nB\n<!-- /mship:task -->\n"
    )
    with pytest.raises(ValueError, match="duplicate task id '1'"):
        extract_plan_task(dup, "1")


def test_extract_unterminated_block_raises():
    bad = "<!-- mship:task id=1 -->\nno closing anchor here\n"
    with pytest.raises(ValueError, match="unterminated"):
        extract_plan_task(bad, "1")


def test_extract_unterminated_when_next_open_precedes_close():
    # task 1 never closes before task 2 opens
    bad = (
        "<!-- mship:task id=1 -->\nA\n"
        "<!-- mship:task id=2 -->\nB\n<!-- /mship:task -->\n"
    )
    with pytest.raises(ValueError, match="unterminated"):
        extract_plan_task(bad, "1")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/core/test_dispatch.py -q`
Expected: FAIL with `ImportError: cannot import name 'extract_plan_task'`.

- [ ] **Step 3: Implement the helper**

In `src/mship/core/dispatch.py`, add `import re` to the imports at the top (alongside `import subprocess`), and add this helper (place it just below the `_CANONICAL_SKILL_NAMES` block, before `canonical_skills`):

```python
_TASK_OPEN_RE = re.compile(r"<!--\s*mship:task\s+id=([^\s>]+)\s*-->")
_TASK_CLOSE_RE = re.compile(r"<!--\s*/mship:task\s*-->")


def extract_plan_task(plan_text: str, task_id: str) -> str:
    """Return the inner content of the anchored task block whose id matches.

    A task block is delimited by `<!-- mship:task id=<task_id> -->` and the
    next `<!-- /mship:task -->`. The returned text is the content between the
    anchors, with surrounding whitespace stripped. Pure — no I/O.

    Raises ValueError when the id is missing, appears more than once, or the
    block is unterminated (no closing anchor before the next open anchor / EOF).
    """
    opens = [m for m in _TASK_OPEN_RE.finditer(plan_text) if m.group(1) == task_id]
    if not opens:
        raise ValueError(
            f"no task with id {task_id!r} in plan "
            f"(expected an anchor `<!-- mship:task id={task_id} -->`)"
        )
    if len(opens) > 1:
        raise ValueError(
            f"duplicate task id {task_id!r} in plan ({len(opens)} anchors)"
        )
    open_m = opens[0]
    close_m = _TASK_CLOSE_RE.search(plan_text, open_m.end())
    next_open = _TASK_OPEN_RE.search(plan_text, open_m.end())
    if close_m is None or (next_open is not None and next_open.start() < close_m.start()):
        raise ValueError(
            f"unterminated task block for id {task_id!r} "
            f"(missing closing `<!-- /mship:task -->`)"
        )
    return plan_text[open_m.end():close_m.start()].strip()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/core/test_dispatch.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/dispatch.py tests/core/test_dispatch.py
git commit -m "feat(dispatch): add pure extract_plan_task helper (MOS-185)"
mship journal "extract_plan_task helper + table tests; passing" --action committed --test-state pass
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2: `mship dispatch --plan/--plan-task` + exactly-one-of instruction source

**Files:**
- Modify: `src/mship/cli/dispatch.py`
- Test: `tests/cli/test_dispatch.py`

**Context:** Today `--instruction/-i` is required. After this task the instruction comes from exactly one of: inline `--instruction "<text>"`, stdin `--instruction -`, or `--plan-task <id>` (with `--plan <path>`). Depends on Task 1's `extract_plan_task`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_dispatch.py` (the file already has `runner`, `_bootstrap`, `_reset`; reuse them). Add `import` for nothing new — these use the existing helpers:

```python
def _override(cfg, state_dir):
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)


def test_dispatch_plan_task_uses_extracted_section(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    plan = tmp_path / "plan.md"
    plan.write_text(
        "<!-- mship:task id=7 -->\n### Task 7\n\nwire the parser\n<!-- /mship:task -->\n"
    )
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app, ["dispatch", "--task", "t", "--plan", str(plan), "--plan-task", "7"]
        )
        assert result.exit_code == 0, result.output
        assert "wire the parser" in result.output
    finally:
        _reset()


def test_dispatch_requires_one_instruction_source(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t"])
        assert result.exit_code != 0
        assert "exactly one instruction source" in result.output
    finally:
        _reset()


def test_dispatch_rejects_two_instruction_sources(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    plan = tmp_path / "plan.md"
    plan.write_text("<!-- mship:task id=1 -->\nx\n<!-- /mship:task -->\n")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app,
            ["dispatch", "--task", "t", "-i", "inline", "--plan", str(plan), "--plan-task", "1"],
        )
        assert result.exit_code != 0
        assert "exactly one instruction source" in result.output
    finally:
        _reset()


def test_dispatch_plan_task_without_plan_errors(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code != 0
        assert "--plan-task requires --plan" in result.output
    finally:
        _reset()


def test_dispatch_instruction_dash_reads_stdin(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "-"], input="from stdin\n")
        assert result.exit_code == 0, result.output
        assert "> from stdin" in result.output
    finally:
        _reset()
```

The existing `test_dispatch_single_repo_task_prints_prompt` (inline `-i "do the thing"`) is the regression guard that inline still works — leave it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/cli/test_dispatch.py -q`
Expected: the new tests FAIL — `--plan`/`--plan-task` are unknown options, and dispatch currently requires `-i`.

- [ ] **Step 3: Implement the CLI changes**

Replace the body of `src/mship/cli/dispatch.py` with:

```python
"""`mship dispatch` — emit an agent-agnostic subagent prompt to stdout.

See docs/superpowers/specs/2026-04-17-mship-dispatch-design.md.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core import dispatch as _d
from mship.core.skill_install import pkg_skills_source


def register(app: typer.Typer, get_container):
    @app.command()
    def dispatch(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug (defaults to cwd-resolved)."),
        repo: Optional[str] = typer.Option(None, "--repo", help="Which repo's worktree to target (multi-repo tasks)."),
        instruction: Optional[str] = typer.Option(
            None, "--instruction", "-i",
            help='Instruction text passed verbatim. Use "-" to read it from stdin.',
        ),
        plan: Optional[Path] = typer.Option(
            None, "--plan", help="Path to an implementation plan with anchored task blocks."
        ),
        plan_task: Optional[str] = typer.Option(
            None, "--plan-task",
            help="Anchor id in --plan to use as the instruction (mutually exclusive with --instruction).",
        ),
    ):
        """Emit a self-contained markdown subagent prompt to stdout.

        Exactly one instruction source is required: inline `--instruction "<text>"`,
        stdin `--instruction -`, or `--plan-task <id>` (with `--plan <path>`).
        """
        output = Output()

        # --- resolve the instruction source (exactly one of) ---
        if (instruction is not None) == (plan_task is not None):
            output.error(
                'provide exactly one instruction source: --instruction "<text>", '
                "--instruction - (stdin), or --plan-task <id> (with --plan)."
            )
            raise typer.Exit(code=2)

        if plan_task is not None:
            if plan is None:
                output.error("--plan-task requires --plan <path>.")
                raise typer.Exit(code=2)
            try:
                plan_text = plan.read_text()
            except OSError as e:
                output.error(f"cannot read plan {str(plan)!r}: {e}")
                raise typer.Exit(code=2)
            try:
                resolved_instruction = _d.extract_plan_task(plan_text, plan_task)
            except ValueError as e:
                output.error(str(e))
                raise typer.Exit(code=2)
        elif instruction == "-":
            resolved_instruction = sys.stdin.read().strip()
            if not resolved_instruction:
                output.error("no instruction read from stdin.")
                raise typer.Exit(code=2)
        else:
            resolved_instruction = instruction  # inline (guaranteed non-None here)

        container = get_container()
        state = container.state_manager().load()
        resolved = resolve_for_command("dispatch", state, task, output)
        task_obj = resolved.task

        try:
            resolved_repo = _d.resolve_repo(task_obj, repo)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        worktree = Path(task_obj.worktrees[resolved_repo])
        base_sha_info = _d.collect_base_sha_info(worktree, task_obj.base_branch or "main")

        log_mgr = container.log_manager()
        journal_entries = log_mgr.read(task_obj.slug, last=10)

        # AGENTS.md lives next to the config file (workspace root).
        config_path = Path(container.config_path())
        agents_md = config_path.parent / "AGENTS.md"
        agents_md_path = agents_md if agents_md.is_file() else None

        prompt = _d.build_dispatch_prompt(
            task=task_obj,
            repo=resolved_repo,
            instruction=resolved_instruction,
            journal_entries=journal_entries,
            base_sha_info=base_sha_info,
            agents_md_path=agents_md_path,
            pkg_skills_source=pkg_skills_source(),
            state=state,
        )
        # Print directly to stdout (NOT via Output.json — this is meant to be piped).
        print(prompt)
```

Note: instruction resolution happens *before* task resolution so a bad instruction source fails fast and the same error text shows regardless of task context.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/cli/test_dispatch.py -q`
Expected: PASS (all, including the pre-existing inline test).

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/dispatch.py tests/cli/test_dispatch.py
git commit -m "feat(dispatch): --plan/--plan-task + exactly-one-of instruction source (MOS-185)"
mship journal "dispatch --plan-task/stdin + exactly-one-of source; passing" --action committed --test-state pass
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3: `mship spec dispatch --task` + idempotent reuse (MOS-181)

**Files:**
- Modify: `src/mship/core/spec_dispatch.py`
- Modify: `src/mship/cli/spec.py` (the `dispatch` command, ~line 381)
- Test: `tests/core/test_spec_dispatch.py`

**Context:** Today `dispatch_spec` only binds a task when `slug == spec.id`, else it auto-spawns — so a task pre-spawned under a different slug yields a duplicate. Add explicit `--task <slug>` binding and make re-dispatch idempotent.

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_spec_dispatch.py` (reuse `_approved_spec`, `_store`, `_sm`, `_task`, `NOW`):

```python
def test_dispatch_spec_binds_existing_differently_slugged_task(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={"other": _task(slug="other")}))
    spec = _approved_spec()  # id == "dq", no task named "dq"
    store.save(spec)

    def spawn_fn(_s):
        raise AssertionError("must not auto-spawn when --task binds an existing task")

    result = dispatch_spec(
        spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW, task_slug="other"
    )

    assert result.spawned is False
    assert result.task.slug == "other"
    assert result.spec.task_slug == "other"
    assert "dq" not in sm.load().tasks                      # no duplicate spawned
    assert sm.load().tasks["other"].spec_id == "dq"         # the existing task is bound


def test_dispatch_spec_unknown_task_errors(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={}))
    spec = _approved_spec()
    store.save(spec)
    with pytest.raises(DispatchError, match="--task"):
        dispatch_spec(
            spec, state_manager=sm, store=store,
            spawn_fn=lambda s: None, now=NOW, task_slug="ghost",
        )


def test_dispatch_spec_is_idempotent_when_already_bound(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={"other": _task(slug="other")}))
    spec = _approved_spec()
    store.save(spec)

    # first dispatch binds to "other"
    dispatch_spec(
        spec, state_manager=sm, store=store,
        spawn_fn=lambda s: (_ for _ in ()).throw(AssertionError("no spawn")),
        now=NOW, task_slug="other",
    )
    # second dispatch (now status == "dispatched", bound) reuses, no spawn, no --task
    result = dispatch_spec(
        spec, state_manager=sm, store=store,
        spawn_fn=lambda s: (_ for _ in ()).throw(AssertionError("no spawn")),
        now=NOW,
    )
    assert result.spawned is False
    assert result.task.slug == "other"
    assert list(sm.load().tasks) == ["other"]               # still exactly one task


def test_dispatch_spec_rebind_conflict_errors(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={
        "other": _task(slug="other"),
        "another": _task(slug="another"),
    }))
    spec = _approved_spec()
    store.save(spec)
    dispatch_spec(
        spec, state_manager=sm, store=store,
        spawn_fn=lambda s: None, now=NOW, task_slug="other",
    )
    with pytest.raises(DispatchError, match="already bound"):
        dispatch_spec(
            spec, state_manager=sm, store=store,
            spawn_fn=lambda s: None, now=NOW, task_slug="another",
        )
```

The existing tests (`…binds_existing_task_without_spawning` for slug==id, `…auto_spawns_when_no_task`, `…requires_approved`, `…auto_spawn_requires_affected_repos`) are regression guards — leave them.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/core/test_spec_dispatch.py -q`
Expected: the new tests FAIL — `dispatch_spec` has no `task_slug` kwarg.

- [ ] **Step 3: Implement `dispatch_spec`**

In `src/mship/core/spec_dispatch.py`, change the `dispatch_spec` signature and body. Replace from the `def dispatch_spec(` line through the `return DispatchResult(...)` at the end with:

```python
def dispatch_spec(
    spec: Spec,
    *,
    state_manager,
    store,
    spawn_fn: Callable[[Spec], Task],
    now: datetime,
    task_slug: str | None = None,
) -> DispatchResult:
    """Bind an approved (or already-dispatched) spec to a task.

    Task selection, in order:
    - `task_slug` given: bind to that existing task (error if unknown, or if the
      spec is already bound to a *different* task).
    - spec already bound (`spec.task_slug` exists in state): reuse it (idempotent).
    - a task named `spec.id` exists: bind to it.
    - otherwise: auto-spawn via `spawn_fn(spec)` (requires `affected_repos`).
    """
    if spec.status not in ("approved", "dispatched"):
        raise DispatchError(
            f"spec {spec.id!r} is {spec.status!r} — approve it first "
            f"(mship spec approve {spec.id})."
        )

    state = state_manager.load()
    bound_slug = (
        spec.task_slug
        if spec.task_slug and spec.task_slug in state.tasks
        else None
    )

    if task_slug is not None:
        if task_slug not in state.tasks:
            raise DispatchError(
                f"--task {task_slug!r} not found. "
                f"Active tasks: {sorted(state.tasks)}."
            )
        if bound_slug is not None and bound_slug != task_slug:
            raise DispatchError(
                f"spec {spec.id!r} is already bound to task {bound_slug!r}; "
                f"refusing to rebind to {task_slug!r}. Drop --task to reuse it."
            )
        task = state.tasks[task_slug]
        spawned = False
    elif bound_slug is not None:
        task = state.tasks[bound_slug]
        spawned = False
    elif spec.id in state.tasks:
        task = state.tasks[spec.id]
        spawned = False
    else:
        if not spec.affected_repos:
            raise DispatchError(
                f"spec {spec.id!r} has no affected_repos; cannot auto-spawn a task. "
                f"Add repos to the spec or spawn a task named {spec.id!r} first."
            )
        task = spawn_fn(spec)
        spawned = True

    # Bind the chosen task to the spec under the state lock.
    chosen_slug = task.slug

    def _bind(s):
        if chosen_slug in s.tasks:
            s.tasks[chosen_slug].spec_id = spec.id

    state_manager.mutate(_bind)

    spec.status = "dispatched"
    spec.task_slug = chosen_slug
    spec.updated_at = now
    store.save(spec)

    return DispatchResult(
        spec=spec, task=task, spawned=spawned,
        handoff=build_dispatch_handoff(spec, task),
    )
```

Also update the docstring above (the bullet list under "Auto-spawns a task…") if it contradicts the new behavior — keep it accurate.

- [ ] **Step 4: Thread `--task` through the CLI**

In `src/mship/cli/spec.py`, the `dispatch` command (~line 382). Add the option and pass it through:

```python
    @spec_app.command("dispatch")
    def dispatch(
        spec_id: str = typer.Argument(..., help="Spec id to dispatch (must be approved)."),
        task_slug: Optional[str] = typer.Option(
            None, "--task",
            help="Bind to this existing task slug instead of auto-spawning a slug==id task.",
        ),
    ):
```

…and in the `dispatch_spec(...)` call inside it, add `task_slug=task_slug,`:

```python
            result = dispatch_spec(
                spec,
                state_manager=container.state_manager(),
                store=store,
                spawn_fn=_spawn,
                now=datetime.now(timezone.utc),
                task_slug=task_slug,
            )
```

Confirm `Optional` is imported at the top of `src/mship/cli/spec.py` (it uses `typer.Option` already; add `from typing import Optional` if not present).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/core/test_spec_dispatch.py tests/cli/test_spec.py -q`
Expected: PASS (new + existing). If `tests/cli/test_spec.py` doesn't exercise dispatch, that's fine — the core tests cover the logic.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/spec_dispatch.py src/mship/cli/spec.py tests/core/test_spec_dispatch.py
git commit -m "feat(spec): spec dispatch --task + idempotent reuse (MOS-181)"
mship journal "spec dispatch --task binding + idempotent reuse; passing" --action committed --test-state pass
```
<!-- /mship:task -->

<!-- mship:task id=4 -->
### Task 4: Skills adopt anchors + `mship test`, with guard tests

**Files:**
- Modify: `src/mship/skills/writing-plans/SKILL.md`
- Modify: `src/mship/skills/subagent-driven-development/SKILL.md`
- Modify: `src/mship/skills/subagent-driven-development/implementer-prompt.md`
- Test: `tests/skills/test_skill_dispatch_ergonomics.py` (new file)

**Context:** Make the bundled skills teach the new workflow so future plan execution uses it by default. Guard tests pin the key strings so the docs can't silently drift from the code.

- [ ] **Step 1: Write the failing guard tests**

Create `tests/skills/test_skill_dispatch_ergonomics.py`:

```python
"""Guard the bundled skills against drifting from the dispatch-ergonomics workflow."""
from __future__ import annotations

from mship.core.skill_install import pkg_skills_source


def _read(skill: str, fname: str = "SKILL.md") -> str:
    return (pkg_skills_source() / skill / fname).read_text()


def test_writing_plans_documents_task_anchors():
    text = _read("writing-plans")
    assert "<!-- mship:task id=" in text
    assert "<!-- /mship:task -->" in text


def test_writing_plans_documents_plan_task_dispatch():
    text = _read("writing-plans")
    assert "--plan-task" in text


def test_sdd_references_plan_task_dispatch():
    text = _read("subagent-driven-development")
    assert "--plan-task" in text


def test_sdd_uses_mship_test_for_evidence():
    text = _read("subagent-driven-development")
    assert "mship test" in text


def test_implementer_prompt_uses_mship_test():
    text = _read("subagent-driven-development", "implementer-prompt.md")
    assert "mship test" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync pytest tests/skills/test_skill_dispatch_ergonomics.py -q`
Expected: FAIL — the anchor markers, `--plan-task`, and `mship test` strings aren't in the skills yet. (`test_sdd_references_plan_task_dispatch` and the implementer-prompt test fail; the anchor tests fail.)

If `tests/skills/` doesn't exist, create it with an empty `__init__.py` only if the test suite requires package dirs (check a sibling like `tests/core/`; if those have no `__init__.py`, skip it).

- [ ] **Step 3: Add anchors + the dispatch command to `writing-plans/SKILL.md`**

In the `## Task Structure` section, wrap the fenced task template so it reads (note the anchors inside the ```` ```markdown ```` block):

````markdown
## Task Structure

````markdown
<!-- mship:task id=N -->
### Task N: [Component Name]

**Files:**
- Create: `exact/path/to/file.py`
- Modify: `exact/path/to/existing.py:123-145`
- Test: `tests/exact/path/to/test.py`

- [ ] **Step 1: Write the failing test**
… (unchanged steps) …
<!-- /mship:task -->
````

Wrap each task in `<!-- mship:task id=N -->` … `<!-- /mship:task -->` anchors (id = the task number). A controller can then pull a single task's text with `mship dispatch --task <slug> --plan <plan-path> --plan-task <N>` instead of hand-assembling the prompt.
````

Then in `## Execution Handoff`, under the "If Subagent-Driven chosen" bullet, add:

```markdown
- Build each implementer prompt with `mship dispatch --task <slug> --plan <plan-path> --plan-task <N>` (the anchored task text becomes the instruction); subagents run `mship test` so `mship finish` has its evidence trail.
```

Keep the existing wording around it; only add the anchors to the template and the two notes.

- [ ] **Step 4: Update `subagent-driven-development/SKILL.md`**

Find the "Inside a mothership workspace:" paragraph (currently around the "Prompt Templates" section, the line beginning `**Inside a mothership workspace:** prefer \`mship dispatch --task <slug> -i ...\``). Replace that paragraph with:

```markdown
**Inside a mothership workspace:** build each implementer prompt with
`mship dispatch --task <slug> --plan <plan-path> --plan-task <N>` and use its
stdout as the subagent's prompt — the anchored task block from the plan becomes
the instruction, wrapped with the worktree path, slug, phase, recent journal,
and per-repo bases. (Use `-i "<text>"` or `-i -` for stdin when you need an
ad-hoc instruction not in a plan; exactly one instruction source is allowed.)
Dispatched subagents MUST run `mship test` (not a bare runner) so
`mship finish` finds the passing-test evidence it gates on. For a spec-driven
kickoff, `mship spec dispatch <id> [--task <slug>]` binds an approved spec to a
task (existing or auto-spawned) and emits a handoff with the acceptance
criteria. See `working-with-mothership` for the `mship dispatch` vs.
`mship context` decision tree.
```

- [ ] **Step 5: Update `implementer-prompt.md`**

In `src/mship/skills/subagent-driven-development/implementer-prompt.md`, the "## Your Job" list currently says `3. Verify implementation works`. Change that line to:

```markdown
    3. Verify implementation works — in a mothership workspace run `mship test`
       (not a bare runner like `pytest`) so `mship finish` keeps the test-evidence trail
```

- [ ] **Step 6: Run the guard tests to verify they pass**

Run: `uv run --no-sync pytest tests/skills/test_skill_dispatch_ergonomics.py -q`
Expected: PASS (5 passed).

- [ ] **Step 7: Commit**

```bash
git add src/mship/skills/writing-plans/SKILL.md src/mship/skills/subagent-driven-development/SKILL.md src/mship/skills/subagent-driven-development/implementer-prompt.md tests/skills/test_skill_dispatch_ergonomics.py
git commit -m "docs(skills): adopt task anchors + mship dispatch --plan-task + mship test (MOS-142/185)"
mship journal "skills teach anchors + --plan-task + mship test; guard tests passing" --action committed --test-state pass
```
<!-- /mship:task -->

---

## After all tasks

- [ ] Run the full suite: `mship test` (must be green).
- [ ] Transition to review: `mship phase review`.
- [ ] Finish: write a PR body (Summary + Test plan referencing the 4 tasks), then `mship finish --body-file <path> --require-tests`.
- [ ] After merge: close MOS-143 as a duplicate of MOS-185; verify MOS-185/142/181 are resolved.

## Self-Review (completed by plan author)

- **Spec coverage:** ac1→Task 1; ac2/ac3→Task 2; ac4→Task 3; ac5→Task 4. All five acceptance criteria map to a task.
- **Placeholder scan:** no TBD/TODO; every code step shows real code and a runnable command with expected output.
- **Type consistency:** `extract_plan_task(plan_text, task_id) -> str` defined in Task 1 and called identically in Task 2; `dispatch_spec(..., task_slug=...)` defined in Task 3 and threaded from the CLI in the same task. Anchor strings (`<!-- mship:task id= -->`, `<!-- /mship:task -->`) and the `--plan-task` / `mship test` literals are consistent between the implementation (Tasks 1–2) and the guard tests (Task 4).
