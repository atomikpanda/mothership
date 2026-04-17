# CLI UX papercuts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-17-cli-ux-papercuts-design.md`

**Goal:** Fix three CLI UX papercuts — `mship finish --body-file -` stdin support (with TTY guard on both `--body -` and `--body-file -`), `mship init --install-hooks` refreshing stale MSHIP-managed hook blocks, and per-hook per-root install-status output (closes #31).

**Architecture:** Three self-contained fixes. `cli/worktree.py` factors out a `_read_stdin_body_or_exit` helper used by both body-source branches. `core/hooks.py` adds an `InstallOutcome` enum, rewrites `_install_one` to compare-and-refresh existing MSHIP blocks, and changes `install_hook`'s signature to return `dict[str, InstallOutcome]`. `cli/init.py` renders one line per hook per root using that outcome map.

**Tech Stack:** Python 3.14, Typer, pytest.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `src/mship/cli/worktree.py` | Add `_read_stdin_body_or_exit` helper; rewrite body-resolution to use it for both `--body -` and `--body-file -` | modify (lines 460-485) |
| `src/mship/core/hooks.py` | Add `InstallOutcome` enum; rewrite `_install_one` for compare-and-refresh; change `install_hook` return type | modify (lines 60-129) |
| `src/mship/cli/init.py` | Render per-hook per-root outcomes from `install_hook`'s new return type | modify (lines 47-59) |
| `tests/cli/test_finish_integration.py` or equivalent | Stdin happy path, TTY error path, empty-stdin rejection, mutex still works | modify/extend |
| `tests/core/test_hooks.py` | Fresh install; all-current no-op; stale refresh; user-content preservation; corrupt-hook skip | modify/extend |
| `tests/cli/test_init.py` | Per-hook per-root output format | modify/extend |

---

## Task 1: `--body-file -` stdin support + TTY guard (TDD)

**Files:**
- Modify: `src/mship/cli/worktree.py`
- Modify: `tests/cli/test_finish_integration.py` (or the file currently testing `mship finish`; find via `grep -l "body_file\\|--body-file" tests/`)

- [ ] **Step 1.1: Locate the current finish body tests**

```bash
grep -l "body_file\|--body-file\|--body " tests/ -r | head
grep -n "def test.*body\|def test.*finish.*body" tests/ -r | head -20
```

Most likely target: `tests/test_finish_integration.py` (integration-level) or `tests/cli/test_finish.py` (may not exist). If no finish test file exists, create `tests/cli/test_finish_body.py` for the new cases.

- [ ] **Step 1.2: Write failing test for `--body-file -` happy path**

Append to the chosen test file:

```python
"""Tests for `mship finish` body resolution: `--body -` and `--body-file -`."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


runner = CliRunner()


def _bootstrap(tmp_path: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    wt = tmp_path / "wt"; wt.mkdir()
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"],
        worktrees={"mothership": wt},
        branch="feat/t", base_branch="main",
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"t": task}))
    return cfg, state_dir


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_finish_body_file_dash_reads_stdin(tmp_path: Path):
    cfg, state_dir = _bootstrap(tmp_path)
    container.config.reset(); container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        # We want the body-file branch to run; we don't actually need finish
        # to succeed end-to-end (it'll error out downstream on missing gh etc.).
        # Assert the body-file branch didn't crash with "No such file or directory: '-'"
        with patch("sys.stdin.isatty", return_value=False):
            result = runner.invoke(
                app, ["finish", "--task", "t", "--body-file", "-"],
                input="Summary\n\nTest plan\n",
            )
        # Any errors downstream are fine; we only care that '-' was NOT
        # treated as a literal file path.
        assert "No such file or directory" not in result.output
        assert "Could not read --body-file" not in result.output
    finally:
        _reset()


def test_finish_body_file_dash_tty_errors(tmp_path: Path):
    cfg, state_dir = _bootstrap(tmp_path)
    container.config.reset(); container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        with patch("sys.stdin.isatty", return_value=True):
            result = runner.invoke(app, ["finish", "--task", "t", "--body-file", "-"])
        assert result.exit_code == 1
        assert "refusing to read body from an interactive TTY" in result.output
    finally:
        _reset()


def test_finish_body_dash_tty_also_errors(tmp_path: Path):
    """Symmetry: existing `--body -` gains the same TTY guard."""
    cfg, state_dir = _bootstrap(tmp_path)
    container.config.reset(); container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        with patch("sys.stdin.isatty", return_value=True):
            result = runner.invoke(app, ["finish", "--task", "t", "--body", "-"])
        assert result.exit_code == 1
        assert "refusing to read body from an interactive TTY" in result.output
    finally:
        _reset()


def test_finish_body_file_dash_empty_stdin_rejected(tmp_path: Path):
    cfg, state_dir = _bootstrap(tmp_path)
    container.config.reset(); container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        with patch("sys.stdin.isatty", return_value=False):
            result = runner.invoke(
                app, ["finish", "--task", "t", "--body-file", "-"],
                input="",
            )
        assert result.exit_code == 1
        assert "PR body is empty" in result.output
    finally:
        _reset()
```

Run: `uv run pytest tests/cli/test_finish_body.py -v`  (or the chosen file name)
Expected: 4 FAIL — `--body-file -` still crashes on `Path("-").read_text()`; `--body -` doesn't yet have a TTY guard.

- [ ] **Step 1.3: Implement `_read_stdin_body_or_exit` helper and wire into both branches**

In `src/mship/cli/worktree.py`, add a module-level helper near the top of the file (after imports, before any command):

```python
def _read_stdin_body_or_exit(output: "Output") -> str:
    """Read PR body from stdin, erroring if stdin is a TTY.

    Used by both `--body -` and `--body-file -` to give them identical semantics.
    """
    import sys
    if sys.stdin.isatty():
        output.error(
            "refusing to read body from an interactive TTY; "
            "pipe or redirect stdin, or use --body-file <path>"
        )
        raise typer.Exit(code=1)
    return sys.stdin.read()
```

Replace the body-resolution block at lines 467-479 with:

```python
        custom_body: Optional[str] = None
        if body is not None:
            if body == "-":
                custom_body = _read_stdin_body_or_exit(output)
            else:
                custom_body = body
        elif body_file is not None:
            if body_file == "-":
                custom_body = _read_stdin_body_or_exit(output)
            else:
                try:
                    custom_body = Path(body_file).read_text()
                except OSError as e:
                    output.error(f"Could not read --body-file {body_file!r}: {e}")
                    raise typer.Exit(code=1)
```

(Removing the inline `import sys as _sys` that's no longer needed since the helper handles it.)

Update the `--body-file` help text on line 423-426 to document the new behavior:

```python
        body_file: Optional[str] = typer.Option(
            None, "--body-file",
            help="Read PR body from this file. Use '-' to read from stdin. "
                 "Mutually exclusive with --body. "
                 "...",  # preserve any existing trailing help text
        ),
```

(If there's no existing trailing text, just end the sentence with the period after "--body.".)

- [ ] **Step 1.4: Run the finish body tests**

Run: `uv run pytest tests/cli/test_finish_body.py -v`  (or chosen filename)
Expected: 4 PASS

- [ ] **Step 1.5: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_finish_body.py
git commit -m "feat(finish): --body-file - reads stdin; TTY guard on both --body - and --body-file -"
```

---

## Task 2: `InstallOutcome` enum + compare-and-refresh in `_install_one` (TDD)

**Files:**
- Modify: `src/mship/core/hooks.py`
- Modify: `tests/core/test_hooks.py`

- [ ] **Step 2.1: Write failing tests for the new outcomes**

Read `tests/core/test_hooks.py` to find its existing structure. Append these tests:

```python
# --- InstallOutcome + refresh behavior (issue-31 follow-up) ---

from mship.core.hooks import InstallOutcome, install_hook


def _make_git_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git" / "hooks").mkdir(parents=True)
    return root


def test_install_hook_fresh_returns_installed_for_each(tmp_path: Path):
    root = _make_git_root(tmp_path)
    outcomes = install_hook(root)
    assert outcomes == {
        "pre-commit": InstallOutcome.installed,
        "post-commit": InstallOutcome.installed,
        "post-checkout": InstallOutcome.installed,
    }


def test_install_hook_second_run_is_up_to_date(tmp_path: Path):
    root = _make_git_root(tmp_path)
    install_hook(root)  # fresh install
    # Capture mtimes, re-run, assert no file writes happened
    hooks_dir = root / ".git" / "hooks"
    mtimes_before = {n: (hooks_dir / n).stat().st_mtime_ns
                     for n in ("pre-commit", "post-commit", "post-checkout")}
    outcomes = install_hook(root)
    assert outcomes == {
        "pre-commit": InstallOutcome.up_to_date,
        "post-commit": InstallOutcome.up_to_date,
        "post-checkout": InstallOutcome.up_to_date,
    }
    mtimes_after = {n: (hooks_dir / n).stat().st_mtime_ns
                    for n in ("pre-commit", "post-commit", "post-checkout")}
    assert mtimes_before == mtimes_after, "up_to_date outcome must not touch file mtimes"


def test_install_hook_refreshes_stale_block(tmp_path: Path):
    root = _make_git_root(tmp_path)
    # Hand-write a post-commit hook with a stale body (references renamed `_log-commit`)
    post_commit = root / ".git" / "hooks" / "post-commit"
    post_commit.write_text(
        "#!/bin/sh\n"
        "# git post-commit hook\n"
        "# MSHIP-BEGIN — managed by mship; edit outside this block is fine\n"
        "if command -v mship >/dev/null 2>&1; then\n"
        "    mship _log-commit || true\n"  # stale — current template is _journal-commit
        "fi\n"
        "# MSHIP-END\n"
    )
    outcomes = install_hook(root)
    assert outcomes["post-commit"] == InstallOutcome.refreshed
    assert outcomes["pre-commit"] == InstallOutcome.installed   # fresh for the other two
    assert outcomes["post-checkout"] == InstallOutcome.installed
    # Current body should now be present
    assert "_journal-commit" in post_commit.read_text()
    assert "_log-commit" not in post_commit.read_text()


def test_install_hook_preserves_user_content_around_block(tmp_path: Path):
    root = _make_git_root(tmp_path)
    post_commit = root / ".git" / "hooks" / "post-commit"
    # User content before AND after our block
    post_commit.write_text(
        "#!/bin/sh\n"
        "# user's own pre-existing logic\n"
        "echo 'user before'\n"
        "\n"
        "# MSHIP-BEGIN — managed by mship; edit outside this block is fine\n"
        "if command -v mship >/dev/null 2>&1; then\n"
        "    mship _log-commit || true\n"  # stale
        "fi\n"
        "# MSHIP-END\n"
        "echo 'user after'\n"
    )
    install_hook(root)
    content = post_commit.read_text()
    assert "echo 'user before'" in content
    assert "echo 'user after'" in content
    assert "_journal-commit" in content   # block refreshed
    assert "_log-commit" not in content   # stale body gone


def test_install_hook_skips_corrupt_hook_missing_end_marker(tmp_path: Path):
    root = _make_git_root(tmp_path)
    post_commit = root / ".git" / "hooks" / "post-commit"
    post_commit.write_text(
        "#!/bin/sh\n"
        "# MSHIP-BEGIN — managed by mship; edit outside this block is fine\n"
        "if command -v mship >/dev/null 2>&1; then\n"
        "    mship _log-commit || true\n"
        "# (end marker missing)\n"
    )
    before = post_commit.read_text()
    outcomes = install_hook(root)
    assert outcomes["post-commit"] == InstallOutcome.skipped_corrupt
    # File untouched
    assert post_commit.read_text() == before
```

Run: `uv run pytest tests/core/test_hooks.py -k install_hook -v`
Expected: 5 FAIL — `InstallOutcome`, the new return type, and refresh/skipped-corrupt behaviors don't exist yet.

- [ ] **Step 2.2: Add `InstallOutcome` enum and rewrite `_install_one`**

In `src/mship/core/hooks.py`, add near the top (after the import block):

```python
from enum import Enum


class InstallOutcome(str, Enum):
    installed = "installed"
    refreshed = "refreshed"
    up_to_date = "up to date"
    skipped_corrupt = "skipped (corrupt block — missing MSHIP-END)"
```

Replace the existing `_install_one` (lines 58-81) with:

```python
def _install_one(git_root: Path, name: str, header: str, body_sh: str) -> InstallOutcome:
    hooks_dir = git_root / ".git" / "hooks"
    if not hooks_dir.exists():
        raise FileNotFoundError(f"git hooks dir not found: {hooks_dir}")

    path = _hook_path(git_root, name)
    new_block = _block(body_sh)

    if not path.exists():
        path.write_text(f"#!/bin/sh\n{header}\n{new_block}")
        _chmod_executable(path)
        return InstallOutcome.installed

    content = path.read_text()
    if HOOK_MARKER_BEGIN not in content:
        # File exists but has no MSHIP block — append ours.
        if not content.endswith("\n"):
            content += "\n"
        if not content.endswith("\n\n"):
            content += "\n"
        content += new_block
        path.write_text(content)
        _chmod_executable(path)
        return InstallOutcome.installed

    # MSHIP block exists. Parse its exact byte range and compare.
    begin_idx = content.index(HOOK_MARKER_BEGIN)
    end_search = content.find(HOOK_MARKER_END, begin_idx)
    if end_search == -1:
        _chmod_executable(path)
        return InstallOutcome.skipped_corrupt

    # Block spans from begin_idx through the newline after HOOK_MARKER_END.
    after_end = content.find("\n", end_search)
    block_end_excl = len(content) if after_end == -1 else after_end + 1
    existing_block = content[begin_idx:block_end_excl]

    if existing_block == new_block:
        _chmod_executable(path)
        return InstallOutcome.up_to_date

    new_content = content[:begin_idx] + new_block + content[block_end_excl:]
    path.write_text(new_content)
    _chmod_executable(path)
    return InstallOutcome.refreshed
```

Update `install_hook` to collect and return the outcomes:

```python
def install_hook(git_root: Path) -> dict[str, InstallOutcome]:
    """Install or refresh pre-commit, post-checkout, and post-commit hooks.

    Returns a mapping of hook name to the install outcome so callers can
    render per-hook status. Idempotent: re-running on an up-to-date hook
    layout is a no-op (no file writes, mtimes preserved).
    """
    outcomes: dict[str, InstallOutcome] = {}
    for name, (header, body) in _HOOKS.items():
        outcomes[name] = _install_one(git_root, name, header, body)
    return outcomes
```

- [ ] **Step 2.3: Run the hook tests**

Run: `uv run pytest tests/core/test_hooks.py -v`
Expected: all PASS (5 new + any pre-existing).

- [ ] **Step 2.4: Commit**

```bash
git add src/mship/core/hooks.py tests/core/test_hooks.py
git commit -m "feat(hooks): InstallOutcome + compare-and-refresh stale MSHIP blocks"
```

---

## Task 3: Per-hook per-root output in `cli/init.py` (#31)

**Files:**
- Modify: `src/mship/cli/init.py`
- Modify: `tests/cli/test_init.py`

- [ ] **Step 3.1: Write failing test for the new output format**

Append to `tests/cli/test_init.py`:

```python
from mship.core.hooks import InstallOutcome


def test_install_hooks_output_per_hook_per_root(tmp_path: Path, monkeypatch):
    """Each hook gets its own output line with outcome suffix."""
    # Minimal workspace with one git root (single-repo shape)
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  only:\n"
        "    path: .\n"
        "    type: service\n"
    )
    (tmp_path / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (tmp_path / ".git" / "hooks").mkdir(parents=True)

    container.config.reset(); container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(tmp_path / ".mothership")
    try:
        result = runner.invoke(app, ["init", "--install-hooks"])
        assert result.exit_code == 0, result.output
        # Expect one line per hook, each containing the hook name, the
        # hooks-dir path, and the outcome ("installed" for fresh)
        for hook_name in ("pre-commit", "post-commit", "post-checkout"):
            assert hook_name in result.output
        assert "installed" in result.output
        assert str(tmp_path / ".git" / "hooks") in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()


def test_install_hooks_refreshed_vs_up_to_date_labels(tmp_path: Path):
    """Second run shows `up to date`; stale block shows `refreshed`."""
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  only:\n"
        "    path: .\n"
        "    type: service\n"
    )
    (tmp_path / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (tmp_path / ".git" / "hooks").mkdir(parents=True)

    container.config.reset(); container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(tmp_path / ".mothership")
    try:
        # First run: fresh install
        runner.invoke(app, ["init", "--install-hooks"])
        # Stale-ify the post-commit hook
        post_commit = tmp_path / ".git" / "hooks" / "post-commit"
        post_commit.write_text(post_commit.read_text().replace("_journal-commit", "_log-commit"))
        # Second run
        result = runner.invoke(app, ["init", "--install-hooks"])
        assert result.exit_code == 0, result.output
        # post-commit should be refreshed; pre-commit and post-checkout up to date
        assert "post-commit" in result.output
        assert "refreshed" in result.output
        assert "up to date" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
```

Run: `uv run pytest tests/cli/test_init.py -k install_hooks -v`
Expected: 2 FAIL — current output format is the old single-line-per-root shape.

- [ ] **Step 3.2: Replace the output loop in `cli/init.py`**

Replace lines 47-59 of `src/mship/cli/init.py` with:

```python
            from mship.core.hooks import InstallOutcome
            installed_results: list[tuple[Path, dict[str, InstallOutcome]]] = []
            failed: list[tuple[Path, str]] = []
            for root in _unique_git_roots(config):
                try:
                    outcomes = install_hook(root)
                    installed_results.append((root, outcomes))
                except Exception as e:
                    failed.append((root, str(e)))
            for root, outcomes in installed_results:
                hooks_dir = root / ".git" / "hooks"
                for hook_name in ("pre-commit", "post-commit", "post-checkout"):
                    outcome = outcomes.get(hook_name)
                    if outcome is None:
                        continue
                    line = f"{hook_name} @ {hooks_dir}/: {outcome.value}"
                    if outcome in (InstallOutcome.installed, InstallOutcome.refreshed):
                        output.success(line)
                    elif outcome is InstallOutcome.skipped_corrupt:
                        output.warning(line)
                    else:
                        output.print(line)
            for r, err in failed:
                output.error(f"hook install failed: {r}: {err}")
            raise typer.Exit(code=1 if failed else 0)
```

- [ ] **Step 3.3: Run the init tests**

Run: `uv run pytest tests/cli/test_init.py -v`
Expected: all PASS.

- [ ] **Step 3.4: Commit**

```bash
git add src/mship/cli/init.py tests/cli/test_init.py
git commit -m "feat(init): per-hook per-root install output with outcome suffix (closes #31)"
```

---

## Task 4: Manual smoke test

**No files modified. Spot-check the user-facing behavior.**

- [ ] **Step 4.1: `--body-file -` pipe**

```bash
cd <this worktree>
printf "Summary\n\nTest plan\n" | uv run mship finish --task $(mship status | jq -r .slug) --body-file - 2>&1 | tail
```

Expected: `finish` proceeds (may fail later on audit/reconcile gates, but the body-file resolution does NOT error with `No such file or directory: '-'`).

- [ ] **Step 4.2: `--body-file -` TTY**

```bash
uv run mship finish --task <slug> --body-file - 2>&1 | head -3
```

Expected: `ERROR: refusing to read body from an interactive TTY; pipe or redirect stdin, or use --body-file <path>`. Exit code 1.

- [ ] **Step 4.3: Hook refresh**

```bash
# Stale-ify the post-commit hook by hand
sed -i 's/_journal-commit/_log-commit/' $(git rev-parse --git-common-dir)/hooks/post-commit
# Run install-hooks
uv run mship init --install-hooks
```

Expected output:
```
pre-commit @ /abs/.git/hooks/: up to date
post-commit @ /abs/.git/hooks/: refreshed
post-checkout @ /abs/.git/hooks/: up to date
```

Verify:
```bash
grep -c "_journal-commit" $(git rev-parse --git-common-dir)/hooks/post-commit   # expect 1
grep -c "_log-commit" $(git rev-parse --git-common-dir)/hooks/post-commit       # expect 0
```

- [ ] **Step 4.4: Second run after refresh**

```bash
uv run mship init --install-hooks
```

Expected: all three lines say `up to date`.

---

## Task 5: Full verification + PR

- [ ] **Step 5.1: Full test suite**

```bash
uv run pytest -x -q
```

Expected: all pass.

- [ ] **Step 5.2: Spec coverage check**

| Spec requirement | Task |
|---|---|
| `--body-file -` reads stdin | 1.3 |
| TTY guard on `--body -` AND `--body-file -` | 1.3 (symmetric helper) |
| Empty stdin → empty-body rejected | 1.2 test |
| `_install_one` compares + refreshes stale MSHIP blocks | 2.2 |
| `InstallOutcome` enum with 4 values | 2.2 |
| `install_hook` returns `dict[str, InstallOutcome]` | 2.2 |
| Per-hook per-root output, closes #31 | 3.2 |
| Corrupt hook (missing MSHIP-END) → `skipped_corrupt`, no crash | 2.2 |
| User content around MSHIP block preserved | 2.1 test |

- [ ] **Step 5.3: Open the PR via `mship finish` with a real body (uses the new `--body-file -` itself — dogfood!)**

```bash
mship finish --body-file - <<'EOF'
## Summary

Three CLI UX papercuts fixed in one PR (closes #31; closes the stale-hook bug and the `--body-file -` missing-stdin bug that surfaced in real usage this session):

- `mship finish --body-file -` now reads the PR body from stdin. A TTY guard (shared with `--body -`) errors fast if stdin is an interactive terminal instead of hanging.
- `mship init --install-hooks` now compares existing MSHIP-managed hook bodies to the current templates and refreshes stale ones (e.g., renames the long-dead `_log-commit` to `_journal-commit` on upgrade). Idempotent — running twice is a no-op.
- `mship init --install-hooks` now prints one line per hook per git root with the outcome (`installed` / `refreshed` / `up to date` / `skipped (corrupt)`), instead of the misleading single "pre-commit" line.

Closes #31.

## Test plan

- [x] Unit (hooks): fresh install, idempotent re-run (mtimes preserved), stale-body refresh, user-content preservation around the MSHIP block, corrupt-block skip.
- [x] CLI (init): per-hook per-root output format; `refreshed` vs `up to date` labels on second run.
- [x] CLI (finish): `--body-file -` happy path, TTY error, empty-stdin rejection, `--body -` TTY symmetry.
- [x] Manual smoke: all four Task 4 steps pass on this workspace.
- [x] Full pytest green.
EOF
```

(This is dogfooding — the PR body is being delivered via `--body-file -`, which is one of the things the PR adds.)
