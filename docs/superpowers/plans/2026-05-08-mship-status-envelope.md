# `mship status` single-envelope JSON shape — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bimodal JSON output of `mship status` (workspace summary OR task detail, shape determined by context) with a single stable envelope that always exposes both views, eliminating the polymorphic-response wart for JSON consumers.

**Architecture:** Refactor `src/mship/cli/status.py` to always build one payload `{workspace, active_tasks, resolved_task | null, resolution_source | null, cwd_is_outside_worktrees}`. The resolved-task detail block (existing top-level keys today) moves under `resolved_task`. TTY rendering is unchanged — it remains context-aware (workspace summary when no task resolves, task-detail block when one does). Hard flip on the wire; no soft-deprecation window.

**Tech Stack:** Python 3, Typer, pytest. No new dependencies.

---

## Spec reference

`docs/superpowers/specs/2026-05-08-mship-status-envelope-design.md` in this worktree.

## File structure

**Modified:**

- `src/mship/cli/status.py` — single-envelope construction; merges the two existing JSON branches.
- `tests/cli/test_status.py` — update existing assertions on top-level task-detail keys; add new envelope-shape tests.
- `tests/test_integration.py` — update `test_full_lifecycle` assertion at line 95.
- `tests/test_init_integration.py` — update `mship init → status` assertion at line 46.
- `src/mship/skills/working-with-mothership/SKILL.md` — update the two JSON examples (lines ~427–432).
- `src/mship/skills/subagent-driven-development/SKILL.md` — update the line about `mship status` returning task detail "directly" (line ~24).

**Not modified:**

- `src/mship/cli/_resolve.py` — `resolve_or_exit` already exists and is fine; we'll use `resolve_task` directly because we need the source enum.
- TTY rendering paths in `status.py` — keep as-is. Polymorphism in TTY is the right UX.
- The `Task` Pydantic model — still serialized via `model_dump(mode="json")`; just placed under `resolved_task`.

---

## Task 1: Add envelope-shape tests (TDD red)

**Files:**

- Modify: `tests/cli/test_status.py`

These tests describe the new envelope and will all fail against the current bimodal implementation.

- [ ] **Step 1: Add envelope-shape tests at the end of `tests/cli/test_status.py`**

```python
# --- Single-envelope JSON shape (#128) ---


def test_status_envelope_zero_tasks(tmp_path, monkeypatch):
    """No tasks → envelope with workspace, empty active_tasks, null resolved_task."""
    _mk_workspace(tmp_path, {})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    try:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["workspace"] == "t"
        assert data["active_tasks"] == []
        assert data["resolved_task"] is None
        assert data["resolution_source"] is None
    finally:
        _reset_container()


def test_status_envelope_multiple_tasks_no_anchor(tmp_path, monkeypatch):
    """2+ active tasks with no anchor → resolved_task null, active_tasks lists all."""
    _mk_workspace(tmp_path, {"A": "dev", "B": "review"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    try:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        slugs = {t["slug"] for t in data["active_tasks"]}
        assert slugs == {"A", "B"}
        assert data["resolved_task"] is None
        assert data["resolution_source"] is None
    finally:
        _reset_container()


def test_status_envelope_resolves_via_task_flag(tmp_path, monkeypatch):
    """`--task X` populates resolved_task with full detail; source = '--task'."""
    _mk_workspace(tmp_path, {"A": "dev", "B": "review"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    try:
        result = runner.invoke(app, ["status", "--task", "A"])
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["resolved_task"] is not None
        assert data["resolved_task"]["slug"] == "A"
        assert data["resolved_task"]["phase"] == "dev"
        assert data["resolution_source"] == "--task"
        # Top-level no longer carries task-detail keys.
        assert "phase" not in data
        assert "slug" not in data
    finally:
        _reset_container()


def test_status_envelope_resolved_task_has_drift_and_last_log(workspace_with_git):
    """resolved_task carries the enriched fields (drift, last_log) the old top-level had."""
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        phase_entered_at=datetime.now(timezone.utc),
        affected_repos=["shared"], branch="feat/t",
    )
    _seed(workspace_with_git, task)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        result = runner.invoke(app, ["status", "--task", "t"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["resolved_task"] is not None
        assert "drift" in data["resolved_task"]
        assert "last_log" in data["resolved_task"]
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_status_envelope_unknown_task_flag_errors(tmp_path, monkeypatch):
    """`--task <unknown>` still errors loudly (unchanged behavior)."""
    _mk_workspace(tmp_path, {"A": "dev"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    try:
        result = runner.invoke(app, ["status", "--task", "nope"])
        assert result.exit_code != 0
        assert "nope" in result.output.lower()
    finally:
        _reset_container()
```

- [ ] **Step 2: Run the new tests, confirm they fail**

```bash
uv run pytest tests/cli/test_status.py -k 'envelope' -v
```

Expected: all 5 tests fail. The first three with `KeyError: 'workspace'` or `assert ... is None` failures because today's response shape doesn't carry `resolved_task`. The fourth with the same `KeyError`. The fifth should already pass (unchanged error behavior) — that's fine.

- [ ] **Step 3: Commit (red)**

```bash
git add tests/cli/test_status.py
git commit -m "test(status): describe single-envelope JSON shape (#128) — red"
mship journal "wrote envelope-shape tests; 4 of 5 fail as expected" --action committed
```

---

## Task 2: Refactor `mship status` to emit the envelope

**Files:**

- Modify: `src/mship/cli/status.py:34-214` (the entire `status()` command body)

This is the core change. Build one payload object that always carries the envelope keys; populate `resolved_task` only when resolution succeeds. Keep TTY rendering identical.

- [ ] **Step 1: Replace the body of `status()` in `src/mship/cli/status.py`**

Find the existing `status` command (starts at line 34). Replace lines 39 through the end of the function (line 214) with the implementation below. Keep the decorator `@app.command()` and signature line `def status(task: ...)` unchanged.

```python
        """Show workspace summary + resolved task detail (when a task can be
        resolved from cwd / MSHIP_TASK / --task).

        Always returns a single envelope shape (#128) — no bimodal output."""
        from datetime import datetime, timezone
        from mship.util.duration import format_relative
        from mship.core.task_resolver import (
            AmbiguousTaskError, NoActiveTaskError, UnknownTaskError, resolve_task,
        )
        import os
        from pathlib import Path

        container = get_container()
        output = Output()
        config = container.config()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        # --- Resolution: capture both task and source. UnknownTaskError still
        # errors loudly (someone passed --task <unknown> or MSHIP_TASK=<unknown>);
        # NoActive / Ambiguous → resolved_task stays None.
        t = None
        source: str | None = None
        try:
            t, source_enum = resolve_task(
                state,
                cli_task=task,
                env_task=os.environ.get("MSHIP_TASK"),
                cwd=Path.cwd(),
            )
            source = source_enum.value
        except UnknownTaskError as e:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            output.error(f"Unknown task: {e.slug}. Known: {known}.")
            raise typer.Exit(1)
        except (NoActiveTaskError, AmbiguousTaskError):
            pass  # leave t / source as None

        # --- Workspace-level data (always in the envelope).
        active = sorted(
            state.tasks.values(),
            key=lambda tt: (tt.phase_entered_at or tt.created_at),
            reverse=True,
        )
        worktree_paths = _collect_worktree_paths(state)
        any_worktrees = bool(worktree_paths)
        cwd_outside = (
            any_worktrees
            and not _cwd_inside_any_worktree(Path.cwd(), worktree_paths)
        )

        # --- Resolved-task detail (only when a task resolved).
        resolved_payload: dict | None = None
        drift_summary = {"has_errors": False, "error_count": 0}
        last_log: dict | None = None
        if t is not None:
            try:
                from mship.core.repo_state import audit_repos
                from mship.core.audit_gate import collect_known_worktree_paths
                shell = container.shell()
                try:
                    known = collect_known_worktree_paths(state_mgr)
                except Exception:
                    known = frozenset()
                report = audit_repos(
                    config, shell, names=t.affected_repos,
                    known_worktree_paths=known, local_only=True,
                )
                errors = [i for r in report.repos for i in r.issues if i.severity == "error"]
                drift_summary = {"has_errors": bool(errors), "error_count": len(errors)}
            except Exception:
                pass
            try:
                entries = container.log_manager().read(t.slug, last=1)
                if entries:
                    e = entries[-1]
                    first_line = e.message.splitlines()[0] if e.message else ""
                    last_log = {"message": first_line[:60], "timestamp": e.timestamp}
            except Exception:
                last_log = None

            resolved_payload = t.model_dump(mode="json")
            resolved_payload["active_repo"] = t.active_repo
            if t.blocked_reason:
                resolved_payload["phase_display"] = (
                    f"{t.phase} (BLOCKED: {t.blocked_reason})"
                )
            if t.finished_at is not None:
                resolved_payload["close_hint"] = "mship close"
            resolved_payload["drift"] = drift_summary
            resolved_payload["last_log"] = (
                {"message": last_log["message"], "timestamp": last_log["timestamp"].isoformat()}
                if last_log is not None else None
            )

        # --- TTY rendering: unchanged. Workspace summary when no task resolves;
        # task-detail block when one does.
        if output.is_tty:
            if t is None:
                if not active:
                    output.print("No active tasks. Run `mship spawn \"description\"`.")
                else:
                    output.print(f"[bold]Active tasks ({len(active)}):[/bold]")
                    for tt in active:
                        phase_rel = (
                            format_relative(tt.phase_entered_at)
                            if tt.phase_entered_at else "—"
                        )
                        output.print(
                            f"  {tt.slug}  "
                            f"phase={tt.phase} (entered {phase_rel})  "
                            f"branch={tt.branch}"
                        )
                    if cwd_outside:
                        output.print(
                            "\n[yellow]⚠ cwd is outside every active task's worktree.[/yellow]"
                        )
                        output.print(
                            "[yellow]  Running tests/git here will not reflect your task's work.[/yellow]"
                        )
                        for tt in active:
                            for repo, path in tt.worktrees.items():
                                output.print(f"  {tt.slug}:{repo} → {path}")
            else:
                output.print(f"[bold]Task:[/bold] {t.slug}")
                if t.finished_at is not None:
                    output.print(
                        f"[yellow]⚠ Finished:[/yellow] {format_relative(t.finished_at)} — run `mship close` after merge"
                    )
                if t.active_repo is not None:
                    output.print(f"[bold]Active repo:[/bold] {t.active_repo}")
                phase_str = t.phase
                if t.phase_entered_at is not None:
                    rel = format_relative(t.phase_entered_at)
                    phase_str = f"{t.phase} (entered {rel})"
                if t.blocked_reason:
                    phase_str = f"{phase_str}  [red]BLOCKED:[/red] {t.blocked_reason}"
                output.print(f"[bold]Phase:[/bold] {phase_str}")
                if t.blocked_at:
                    output.print(f"[bold]Blocked since:[/bold] {t.blocked_at}")
                output.print(f"[bold]Branch:[/bold] {t.branch}")
                output.print(f"[bold]Repos:[/bold] {', '.join(t.affected_repos)}")
                if t.worktrees:
                    output.print("[bold]Worktrees:[/bold]")
                    for repo, path in t.worktrees.items():
                        output.print(f"  {repo}: {path}")
                if t.test_results:
                    output.print("[bold]Tests:[/bold]")
                    for repo, result in t.test_results.items():
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
            return

        # --- JSON envelope (single shape, always).
        envelope = {
            "workspace": config.workspace,
            "active_tasks": [
                {
                    "slug": tt.slug,
                    "phase": tt.phase,
                    "branch": tt.branch,
                    "phase_entered_at": (
                        tt.phase_entered_at.isoformat()
                        if tt.phase_entered_at else None
                    ),
                }
                for tt in active
            ],
            "resolved_task": resolved_payload,
            "resolution_source": source,
        }
        if any_worktrees:
            envelope["cwd_is_outside_worktrees"] = cwd_outside
        output.json(envelope)
```

- [ ] **Step 2: Run the envelope tests; confirm 4 of 5 now pass**

```bash
uv run pytest tests/cli/test_status.py -k 'envelope' -v
```

Expected: 5 of 5 pass.

- [ ] **Step 3: Run the full status test file; confirm pre-existing tests fail**

```bash
uv run pytest tests/cli/test_status.py -v
```

Expected: many failures in pre-existing tests that asserted top-level task-detail keys (`phase_entered_at`, `drift`, `last_log`, `slug`, `active_repo`, `finished_at`, `cwd_is_outside_worktrees` on the resolved-task path). These will be updated in Task 3. Do not commit yet — fix the tests first to keep the working tree internally consistent.

---

## Task 3: Update existing `tests/cli/test_status.py` assertions

**Files:**

- Modify: `tests/cli/test_status.py` — replace the assertions in pre-existing tests so they read from `resolved_task` where appropriate.

- [ ] **Step 1: Update `test_status_no_task` — workspace shape now has the envelope**

Find the existing test (line 35–41). Replace its body:

```python
def test_status_no_task(configured_app):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["active_tasks"] == []
    assert payload["resolved_task"] is None
    assert payload["resolution_source"] is None
```

- [ ] **Step 2: Update `test_status_cwd_outside_worktrees_reports_true`**

The existing assertion at line 65 (`assert payload.get("cwd_is_outside_worktrees") is True`) still works — the field's location is unchanged in the envelope. No edit needed; just confirm.

- [ ] **Step 3: Update `test_status_cwd_inside_worktree_reports_false`** — same as above; no edit needed.

- [ ] **Step 4: Update `test_status_no_active_tasks_omits_cwd_field`** — no edit needed (existing behavior preserved).

- [ ] **Step 5: Update `test_status_with_task` — TTY-mode test, no edit needed**

The test invokes runner without forcing TTY, so output is JSON. It currently asserts `"add-labels" in result.output` and `"dev" in result.output`. Both still appear (under `resolved_task.slug` and `resolved_task.phase`). The string-substring assertion still passes.

- [ ] **Step 6: Update `test_status_shows_phase_duration_and_drift`** (lines 137–158)

Replace assertions at lines 152–153:

```python
        result = runner.invoke(app, ["status", "--task", "t"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        rt = payload["resolved_task"]
        assert rt is not None
        assert rt["phase_entered_at"] is not None  # phase duration encoded
        assert "drift" in rt  # drift field present
```

- [ ] **Step 7: Update `test_status_json_includes_new_fields`** (lines 161–185)

Replace assertions at lines 177–180:

```python
        result = runner.invoke(app, ["status", "--task", "t"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        rt = payload["resolved_task"]
        assert rt is not None
        assert rt["finished_at"] is not None
        assert rt["phase_entered_at"] is not None
        assert "drift" in rt
        assert "last_log" in rt
```

- [ ] **Step 8: Update `test_status_shows_finished_warning`** (lines 188–208)

This is a TTY-output assertion (`"Finished" in result.output`, `"mship close" in result.output`). TTY rendering is unchanged, so no edit. Confirm by running.

- [ ] **Step 9: Update `test_status_shows_active_repo`** (lines 211–236)

Replace the JSON assertion at lines 226–228:

```python
        try:
            payload = _j.loads(result.output)
            rt = payload["resolved_task"]
            assert rt is not None
            assert rt["active_repo"] == "shared"
        except _j.JSONDecodeError:
            assert "Active repo" in result.output
            assert "shared" in result.output
```

- [ ] **Step 10: Update `test_status_no_tasks_emits_empty_active_list`** (lines 275–287)

Replace the assertion at line 285:

```python
        data = json.loads(result.stdout)
        assert data["workspace"] == "t"
        assert data["active_tasks"] == []
        assert data["resolved_task"] is None
        assert data["resolution_source"] is None
```

- [ ] **Step 11: Update `test_status_multiple_tasks_no_anchor_lists_all`** (lines 290–305)

The existing assertions (`set(slugs) == {"A", "B"}`, per-task keys) still apply because `active_tasks[]` shape is unchanged. Add a sanity check on `resolved_task`:

```python
        data = json.loads(result.stdout)
        slugs = [t["slug"] for t in data["active_tasks"]]
        assert set(slugs) == {"A", "B"}
        for t in data["active_tasks"]:
            assert "slug" in t and "phase" in t and "branch" in t
        # Multiple active, no anchor → no resolution.
        assert data["resolved_task"] is None
```

- [ ] **Step 12: Update `test_status_resolves_via_task_flag`** (lines 308–320)

Replace the assertion at line 318:

```python
        data = json.loads(result.stdout)
        assert data["resolved_task"]["slug"] == "A"
        assert data["resolution_source"] == "--task"
```

- [ ] **Step 13: Run the full status test file; confirm all green**

```bash
uv run pytest tests/cli/test_status.py -v
```

Expected: all tests pass.

- [ ] **Step 14: Commit (envelope green for status tests)**

```bash
git add src/mship/cli/status.py tests/cli/test_status.py
git commit -m "feat(status): single-envelope JSON shape; tests updated (#128)"
mship journal "status now emits envelope; status test file green" --action committed
```

---

## Task 4: Update integration tests

**Files:**

- Modify: `tests/test_integration.py:95`
- Modify: `tests/test_init_integration.py:46`

- [ ] **Step 1: Update `tests/test_integration.py:95`**

Find this line:

```python
    assert payload == {"active_tasks": []}
```

Replace with:

```python
    # Envelope shape (#128): always has workspace + active_tasks + resolved_task.
    assert payload["active_tasks"] == []
    assert payload["resolved_task"] is None
    assert payload["resolution_source"] is None
```

- [ ] **Step 2: Update `tests/test_init_integration.py:46`**

Find this line:

```python
    assert _json.loads(result.output) == {"active_tasks": []}
```

Replace with:

```python
    # Envelope shape (#128).
    payload = _json.loads(result.output)
    assert payload["active_tasks"] == []
    assert payload["resolved_task"] is None
```

- [ ] **Step 3: Run both integration tests; confirm green**

```bash
uv run pytest tests/test_integration.py tests/test_init_integration.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py tests/test_init_integration.py
git commit -m "test(status): adjust integration tests to envelope shape (#128)"
mship journal "integration tests updated for envelope shape" --action committed
```

---

## Task 5: Update bundled skill docs

**Files:**

- Modify: `src/mship/skills/working-with-mothership/SKILL.md` (around lines 425–435 in the JSON-examples block)
- Modify: `src/mship/skills/subagent-driven-development/SKILL.md` (around line 24, the line about task detail "directly")

- [ ] **Step 1: Update `working-with-mothership/SKILL.md` JSON examples**

Find this block (around lines 425–435):

```markdown
```bash
# If you're inside a task's worktree or have MSHIP_TASK set,
# `mship status` returns the resolved task's detail:
mship status | jq -r .phase

# With 0 or 2+ active tasks and no anchor, `mship status` returns a
# workspace summary instead; use this to list active task slugs:
mship status | jq '.active_tasks[].slug'

mship journal | jq '.entries[].message'
mship graph | jq '.order'
```
```

Replace with:

```markdown
```bash
# `mship status` always returns the same envelope shape (#128). When a
# task can be resolved from context (cwd / MSHIP_TASK / --task), its
# full detail is under `.resolved_task`:
mship status | jq -r .resolved_task.phase
mship status | jq -r '.resolved_task.worktrees."<repo>"'

# `.active_tasks[]` is always present — use it to list active slugs:
mship status | jq '.active_tasks[].slug'

# `.resolution_source` tells you how the task was resolved
# ("cwd" | "MSHIP_TASK" | "--task" | "only active task"), or null
# when no task resolved:
mship status | jq -r .resolution_source

mship journal | jq '.entries[].message'
mship graph | jq '.order'
```
```

- [ ] **Step 2: Update `subagent-driven-development/SKILL.md` line 24**

Find this line:

```markdown
- Otherwise `mship status` returns the resolved task's detail directly (keys
```

Read the surrounding context (3–4 lines before and after) to understand the bullet's full meaning, then replace the bullet's content with text that reflects the envelope shape. The replacement should describe that `mship status | jq .resolved_task` returns the resolved task's detail, with `.resolved_task` being null when no task can be resolved. Keep the surrounding bullets intact.

If the surrounding text references specific task-detail keys (`worktrees`, `slug`), update them to `.resolved_task.worktrees`, `.resolved_task.slug`, etc.

- [ ] **Step 3: Verify no other in-tree skill docs reference the old shape**

```bash
grep -rn 'mship status | jq' src/mship/skills/
```

Expected: no remaining references to the old top-level key access (`mship status | jq .phase` etc.). All occurrences should now use `.resolved_task.X`.

- [ ] **Step 4: Commit**

```bash
git add src/mship/skills/working-with-mothership/SKILL.md src/mship/skills/subagent-driven-development/SKILL.md
git commit -m "docs(skills): mship status envelope shape in bundled skills (#128)"
mship journal "bundled skill docs updated for envelope shape" --action committed
```

---

## Task 6: Full-suite verification + finish

**Files:** none modified — verification + ship.

- [ ] **Step 1: Run the full pytest suite**

```bash
mship test 2>&1 | tail -10
```

Expected: status `pass`, no new failures, no regressions.

- [ ] **Step 2: Manual TTY smoke test — workspace summary unchanged for humans**

```bash
mship status
```

Expected (in this worktree): the existing `Task: status-envelope` block (since you're inside the worktree, the task auto-resolves). Phase, branch, repos, drift, last log all rendered as today.

- [ ] **Step 3: Manual JSON smoke test — envelope shape**

```bash
mship status | jq 'keys'
mship status | jq -r .workspace
mship status | jq -r .resolved_task.slug
mship status | jq -r .resolution_source
```

Expected: keys include `workspace`, `active_tasks`, `resolved_task`, `resolution_source`, `cwd_is_outside_worktrees`. `resolved_task.slug` is `status-envelope`. `resolution_source` is `cwd`.

- [ ] **Step 4: Transition to review**

```bash
mship phase review
```

Expected: success, no `BLOCKED` or `Tests not run` warnings. (Iteration tests already ran in Step 1.)

- [ ] **Step 5: Drop unrelated `uv.lock` if present**

```bash
git status -s
git checkout -- uv.lock 2>/dev/null
git status -s
```

Expected: only the intentional changes from this plan are staged/committed.

- [ ] **Step 6: Finish with a real PR body**

Write the PR body to `/tmp/status-envelope-body.md`. Use this content:

```markdown
## Summary

Closes #128. Replaces `mship status`'s bimodal JSON output with a single
stable envelope:

```json
{
  "workspace": "...",
  "active_tasks": [{slug, phase, branch, phase_entered_at}, ...],
  "resolved_task": null | {full task detail + drift + last_log},
  "resolution_source": null | "cwd" | "MSHIP_TASK" | "--task" | "only active task",
  "cwd_is_outside_worktrees": false
}
```

When a task resolves from cwd / `MSHIP_TASK` / `--task`, `resolved_task`
carries the full detail today's `mship status` returns at the top level.
When no task resolves (zero active, multiple active without anchor),
`resolved_task` is `null` — no error, just no resolution.

TTY rendering is unchanged: humans see the existing context-aware output.
The fix is on the JSON wire only.

## BREAKING CHANGE

Scripts that read top-level task-detail keys from `mship status` JSON
break and must migrate:

```diff
-mship status | jq -r .phase
+mship status | jq -r .resolved_task.phase

-mship status | jq -r '.worktrees."myrepo"'
+mship status | jq -r '.resolved_task.worktrees."myrepo"'
```

Same pattern for `.slug`, `.branch`, `.drift`, `.last_log`, `.active_repo`,
etc. `.active_tasks[]` is unchanged.

The bundled `working-with-mothership` and `subagent-driven-development`
skills are updated in this PR so the documented examples are correct
from merge.

## Test plan

- [x] `tests/cli/test_status.py::test_status_envelope_*` — 5 new envelope-shape tests.
- [x] `tests/cli/test_status.py` — pre-existing tests updated to read from `.resolved_task` where applicable.
- [x] `tests/test_integration.py::test_full_lifecycle` — updated to envelope shape.
- [x] `tests/test_init_integration.py` — updated to envelope shape.
- [x] `mship test` (full pytest suite) — all green, no regressions.
- [x] Manual TTY smoke test: rendering unchanged.
- [x] Manual JSON smoke test: envelope keys present; `resolution_source` reflects `cwd` inside the worktree.
```

Then run:

```bash
mship finish --body-file /tmp/status-envelope-body.md
```

Expected: PR opens at `https://github.com/atomikpanda/mothership/pull/<N>`.

- [ ] **Step 7: Report**

Report the PR URL and a one-line summary of what was changed back to the user. After merge, the user runs `mship close --task status-envelope --yes`.

---

## Self-review (writer-side checklist)

- **Spec coverage:** Each section of the spec maps to a task: design (Task 2), wire shape change (Task 2), TTY unchanged (Task 2), test plan (Tasks 1, 3, 4), docs (Task 5), risk/rollout (Task 6 PR body). ✓
- **Placeholder scan:** No "TBD", "fill in", or "similar to Task N". Every code step shows the exact code. ✓
- **Type consistency:** `resolved_task` referenced uniformly across plan/spec/tests. `resolution_source` values match the `ResolutionSource` enum (`--task`, `MSHIP_TASK`, `cwd`, `only active task`). ✓
- **Bite-sized:** Steps are ~2–5 minutes each (one assertion update, one code block, one command). ✓
