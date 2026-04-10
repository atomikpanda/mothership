# `mship finish` PR Creation & Superpowers Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `mship finish` stub with real PR creation via `gh` CLI, and create a superpowers skill that teaches agents how to use mothership.

**Architecture:** New `PRManager` core service handles `gh` CLI interactions. The `finish` CLI command orchestrates PR creation in dependency order, then updates all PRs with cross-reference coordination blocks. The skill is a standalone `SKILL.md` file at `skills/working-with-mothership/`.

**Tech Stack:** Python 3.14, Typer, `gh` CLI (external dependency), PyYAML (existing)

---

## File Map

### Core
- `src/mship/core/state.py` — modify: add `pr_urls` field to Task
- `src/mship/core/pr.py` — create: PRManager for `gh` CLI integration

### CLI
- `src/mship/cli/worktree.py` — modify: replace finish stub with PR creation

### DI
- `src/mship/container.py` — modify: add PRManager provider

### Skill
- `skills/working-with-mothership/SKILL.md` — create: agent guidance skill

### Tests
- `tests/core/test_state.py` — modify: test pr_urls field
- `tests/core/test_pr.py` — create: PRManager tests
- `tests/cli/test_worktree.py` — modify: test finish with mocked gh

---

### Task 1: Add `pr_urls` Field to Task Model

**Files:**
- Modify: `src/mship/core/state.py:15-25`
- Modify: `tests/core/test_state.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/core/test_state.py`:
```python
def test_task_pr_urls_default_empty(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="test",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/test",
    )
    assert task.pr_urls == {}


def test_task_pr_urls_roundtrip(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="test",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/test",
        pr_urls={"shared": "https://github.com/org/shared/pull/18"},
    )
    state = WorkspaceState(current_task="test", tasks={"test": task})
    mgr.save(state)
    loaded = mgr.load()
    assert loaded.tasks["test"].pr_urls["shared"] == "https://github.com/org/shared/pull/18"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_state.py -v -k "pr_urls"`
Expected: FAIL — `pr_urls` is not a valid field

- [ ] **Step 3: Add `pr_urls` field to Task model**

In `src/mship/core/state.py`, add after `blocked_at`:

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_state.py -v -k "pr_urls"`
Expected: Both tests PASS.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/state.py tests/core/test_state.py
git commit -m "feat: add pr_urls field to Task model"
```

---

### Task 2: PRManager Core

**Files:**
- Create: `src/mship/core/pr.py`
- Create: `tests/core/test_pr.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_pr.py`:
```python
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.pr import PRManager
from mship.util.shell import ShellRunner, ShellResult


@pytest.fixture
def mock_shell() -> MagicMock:
    shell = MagicMock(spec=ShellRunner)
    return shell


def test_check_gh_available_success(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="Logged in", stderr="")
    mgr = PRManager(mock_shell)
    mgr.check_gh_available()  # should not raise


def test_check_gh_available_not_installed(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=127, stdout="", stderr="command not found")
    mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="gh"):
        mgr.check_gh_available()


def test_check_gh_available_not_authenticated(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="not logged in")
    mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="auth"):
        mgr.check_gh_available()


def test_push_branch(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    mgr.push_branch(Path("/tmp/repo"), "feat/test")
    mock_shell.run.assert_called_once()
    cmd = mock_shell.run.call_args.args[0]
    assert "git push" in cmd
    assert "feat/test" in cmd


def test_create_pr(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(
        returncode=0,
        stdout="https://github.com/org/repo/pull/42\n",
        stderr="",
    )
    mgr = PRManager(mock_shell)
    url = mgr.create_pr(
        repo_path=Path("/tmp/repo"),
        branch="feat/test",
        title="Add labels",
        body="Task description",
    )
    assert url == "https://github.com/org/repo/pull/42"
    cmd = mock_shell.run.call_args.args[0]
    assert "gh pr create" in cmd


def test_create_pr_failure(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="error")
    mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="Failed to create PR"):
        mgr.create_pr(Path("/tmp/repo"), "feat/test", "title", "body")


def test_update_pr_body(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    mgr.update_pr_body("https://github.com/org/repo/pull/42", "new body")
    cmd = mock_shell.run.call_args.args[0]
    assert "gh pr edit" in cmd


def test_get_pr_body(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="existing body\n", stderr="")
    mgr = PRManager(mock_shell)
    body = mgr.get_pr_body("https://github.com/org/repo/pull/42")
    assert body == "existing body"


def test_build_coordination_block():
    mgr = PRManager(MagicMock())
    prs = [
        {"repo": "shared", "url": "https://github.com/org/shared/pull/18", "order": 1},
        {"repo": "auth-service", "url": "https://github.com/org/auth/pull/42", "order": 2},
    ]
    block = mgr.build_coordination_block("add-labels", prs, current_repo="auth-service")
    assert "add-labels" in block
    assert "shared" in block
    assert "auth-service" in block
    assert "merge first" in block
    assert "this PR" in block


def test_build_coordination_block_single_repo():
    mgr = PRManager(MagicMock())
    prs = [
        {"repo": "shared", "url": "https://github.com/org/shared/pull/18", "order": 1},
    ]
    block = mgr.build_coordination_block("add-labels", prs, current_repo="shared")
    assert block == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_pr.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

`src/mship/core/pr.py`:
```python
import shlex
from pathlib import Path

from mship.util.shell import ShellRunner


class PRManager:
    """Create and manage PRs via the gh CLI."""

    def __init__(self, shell: ShellRunner) -> None:
        self._shell = shell

    def check_gh_available(self) -> None:
        result = self._shell.run("gh auth status", cwd=Path("."))
        if result.returncode == 127:
            raise RuntimeError(
                "gh CLI not found. Install it: https://cli.github.com"
            )
        if result.returncode != 0:
            raise RuntimeError(
                "gh CLI not authenticated. Run `gh auth login` first."
            )

    def push_branch(self, repo_path: Path, branch: str) -> None:
        result = self._shell.run(
            f"git push -u origin {shlex.quote(branch)}",
            cwd=repo_path,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to push branch '{branch}': {result.stderr.strip()}"
            )

    def create_pr(
        self, repo_path: Path, branch: str, title: str, body: str
    ) -> str:
        safe_title = shlex.quote(title)
        safe_body = shlex.quote(body)
        result = self._shell.run(
            f"gh pr create --title {safe_title} --body {safe_body} --head {shlex.quote(branch)}",
            cwd=repo_path,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create PR: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def get_pr_body(self, pr_url: str) -> str:
        result = self._shell.run(
            f"gh pr view {shlex.quote(pr_url)} --json body -q .body",
            cwd=Path("."),
        )
        return result.stdout.strip()

    def update_pr_body(self, pr_url: str, body: str) -> None:
        safe_body = shlex.quote(body)
        self._shell.run(
            f"gh pr edit {shlex.quote(pr_url)} --body {safe_body}",
            cwd=Path("."),
        )

    def build_coordination_block(
        self,
        task_slug: str,
        prs: list[dict],
        current_repo: str,
    ) -> str:
        """Build the coordination block for a PR body.

        Returns empty string for single-repo tasks.
        """
        if len(prs) <= 1:
            return ""

        lines = [
            "",
            "---",
            "",
            "## Cross-repo coordination (mothership)",
            "",
            f"This PR is part of a coordinated change: `{task_slug}`",
            "",
            "| # | Repo | PR | Merge order |",
            "|---|------|----|-------------|",
        ]

        for pr in prs:
            if pr["repo"] == current_repo:
                order_label = "this PR"
            elif pr["order"] == 1:
                order_label = "merge first"
            else:
                order_label = f"merge #{pr['order']}"
            lines.append(
                f"| {pr['order']} | {pr['repo']} | {pr['url']} | {order_label} |"
            )

        # Add merge order warning
        deps_note = " → ".join(pr["repo"] for pr in prs)
        lines.append("")
        lines.append(f"⚠ Merge in order: {deps_note}")

        return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_pr.py -v`
Expected: All 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/pr.py tests/core/test_pr.py
git commit -m "feat: add PRManager for gh CLI integration"
```

---

### Task 3: Wire PRManager and Update `finish` Command

**Files:**
- Modify: `src/mship/container.py`
- Modify: `src/mship/cli/worktree.py`
- Modify: `tests/cli/test_worktree.py`

- [ ] **Step 1: Add PRManager to container**

Add to `src/mship/container.py`:

```python
from mship.core.pr import PRManager
```

Inside Container class, add:

```python
    pr_manager = providers.Factory(
        PRManager,
        shell=shell,
    )
```

- [ ] **Step 2: Replace the finish stub in `src/mship/cli/worktree.py`**

Replace the `finish` function (lines 90-146) with:

```python
    @app.command()
    def finish(
        handoff: bool = typer.Option(False, "--handoff", help="Generate CI handoff manifest"),
    ):
        """Create PRs across repos in dependency order."""
        from pathlib import Path

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

        # PR creation flow
        pr_mgr = container.pr_manager()

        try:
            pr_mgr.check_gh_available()
        except RuntimeError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        pr_list: list[dict] = []

        for i, repo_name in enumerate(ordered, 1):
            # Skip if PR already created (idempotent re-run)
            if repo_name in task.pr_urls:
                output.print(f"  {repo_name}: already has PR {task.pr_urls[repo_name]}")
                pr_list.append({
                    "repo": repo_name,
                    "url": task.pr_urls[repo_name],
                    "order": i,
                })
                continue

            repo_config = config.repos[repo_name]

            # Push branch
            try:
                pr_mgr.push_branch(repo_config.path, task.branch)
            except RuntimeError as e:
                output.error(f"{repo_name}: {e}")
                raise typer.Exit(code=1)

            # Create PR
            try:
                pr_url = pr_mgr.create_pr(
                    repo_path=repo_config.path,
                    branch=task.branch,
                    title=task.description,
                    body=task.description,
                )
            except RuntimeError as e:
                output.error(f"{repo_name}: {e}")
                raise typer.Exit(code=1)

            # Store in state (crash-safe: save after each PR)
            task.pr_urls[repo_name] = pr_url
            state_mgr.save(state)

            pr_list.append({"repo": repo_name, "url": pr_url, "order": i})

            if output.is_tty:
                output.success(f"  {repo_name}: {pr_url}")

        # Update PRs with coordination blocks (multi-repo only)
        if len(pr_list) > 1:
            for pr_info in pr_list:
                block = pr_mgr.build_coordination_block(
                    task.slug, pr_list, current_repo=pr_info["repo"]
                )
                if block:
                    existing_body = pr_mgr.get_pr_body(pr_info["url"])
                    new_body = existing_body + block
                    pr_mgr.update_pr_body(pr_info["url"], new_body)

        # Log PR URLs
        log_mgr = container.log_manager()
        for pr_info in pr_list:
            log_mgr.append(
                state.current_task,
                f"PR created for {pr_info['repo']}: {pr_info['url']}",
            )

        if output.is_tty:
            output.print("")
            output.success(f"Created {len(pr_list)} PR(s) for task: {task.slug}")
            if len(pr_list) > 1:
                output.print("Merge in dependency order as shown in each PR description.")
        else:
            output.json({
                "task": task.slug,
                "prs": pr_list,
            })
```

- [ ] **Step 3: Write CLI tests**

Add to `tests/cli/test_worktree.py`:

```python
from unittest.mock import MagicMock
from mship.util.shell import ShellResult


def test_finish_creates_prs(configured_git_app: Path):
    from mship.cli import container as cli_container

    mock_shell = MagicMock(spec=ShellRunner)
    # gh auth status
    mock_shell.run.side_effect = [
        ShellResult(returncode=0, stdout="Logged in", stderr=""),  # gh auth status
    ]

    # For run_task calls (spawn setup tasks), keep the existing mock behavior
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    runner.invoke(app, ["spawn", "test prs", "--repos", "shared"])

    # Now mock shell for finish
    call_count = 0
    def mock_run(cmd, cwd, env=None):
        nonlocal call_count
        call_count += 1
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        if "gh pr view" in cmd:
            return ShellResult(returncode=0, stdout="body text", stderr="")
        if "gh pr edit" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    cli_container.shell.override(MagicMock(spec=ShellRunner))
    shell_mock = cli_container.shell()
    shell_mock.run.side_effect = mock_run
    shell_mock.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    cli_container.shell.reset_override()


def test_finish_gh_not_available(configured_git_app: Path):
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "test no gh", "--repos", "shared"])

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=127, stdout="", stderr="command not found")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish"])
    assert result.exit_code != 0 or "gh" in result.output.lower()

    cli_container.shell.reset_override()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cli/test_worktree.py -v`
Expected: All tests PASS (existing + new).

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/container.py src/mship/cli/worktree.py tests/cli/test_worktree.py
git commit -m "feat: replace finish stub with real PR creation via gh CLI"
```

---

### Task 4: Superpowers Skill — `working-with-mothership`

**Files:**
- Create: `skills/working-with-mothership/SKILL.md`

- [ ] **Step 1: Create the skill directory**

```bash
mkdir -p skills/working-with-mothership
```

- [ ] **Step 2: Write the skill file**

`skills/working-with-mothership/SKILL.md`:

```markdown
---
name: working-with-mothership
description: Use when working in a workspace with mothership.yaml — provides cross-repo coordination, phase-based workflow, and worktree management via the mship CLI
---

# Working with Mothership

## Overview

Mothership (`mship`) is a CLI tool that provides phase-based workflow orchestration, coordinated worktree management, and structured task execution. It works for single-repo and multi-repo workspaces.

**You are the brain. Mothership is the coordinator. go-task is the muscle.**

- You decide what to build and how to build it
- Mothership tracks phases, manages worktrees, coordinates PRs across repos
- go-task (via Taskfile.yml per repo) runs the actual build/test/lint commands

**Announce at start:** "I'm using the working-with-mothership skill for workspace coordination."

## Session Start Protocol

**Every session, before doing anything else:**

```bash
mship status    # What task am I on? What phase? Am I blocked?
mship log       # What was I doing before this session?
```

If `mship status` fails with "No mothership.yaml found", you're not in a mothership workspace. Skip this skill.

If there's no active task, ask the user what they want to work on, then `mship spawn`.

## Phase Workflow

Mothership enforces a phase progression. Always transition phases explicitly.

| Phase | What happens | Superpowers skill |
|-------|-------------|-------------------|
| `plan` | Brainstorm, write spec, create plan | brainstorming, writing-plans |
| `dev` | Implement the plan, write tests, commit | test-driven-development, subagent-driven-development |
| `review` | Review code, run full test suite | requesting-code-review, verification-before-completion |
| `run` | Deploy, run services, verify in environment | verification-before-completion |

**Transition with:** `mship phase <target>`

Mothership warns (but doesn't block) if preconditions aren't met:
- Entering `dev` without a spec → warning
- Entering `review` without passing tests → warning
- Entering `run` with uncommitted changes → warning

**Respect the warnings.** If mothership warns about missing tests, run `mship test` before proceeding.

## Command Reference

### Starting work

```bash
mship init                          # First time: set up workspace (interactive)
mship init --name my-app --repo ./:service  # Non-interactive setup
mship spawn "add user avatars"      # Create worktrees for a new task
mship spawn "fix auth" --repos shared,auth-service  # Specific repos only
```

### During work

```bash
mship phase dev                     # Transition to development phase
mship test                          # Run tests across repos (dependency order)
mship test --all                    # Run all even if one fails
mship log "refactored auth controller, tests passing"  # Leave breadcrumbs
mship status                        # Check current state
```

### When blocked

```bash
mship block "waiting on API key from ops team"  # Park the task
mship unblock                       # Resume when unblocked
```

### Finishing work

```bash
mship phase review                  # Move to review phase
mship test                          # Verify all tests pass
mship finish                        # Create coordinated PRs
mship abort --yes                   # Clean up worktrees after merge
```

### Workspace awareness

```bash
mship graph                         # Show repo dependency graph
mship prune                         # Find orphaned worktrees (dry-run)
mship prune --force                 # Clean up orphaned worktrees
```

## Context Recovery

When your context is wiped (new session, crash, token limit):

1. Run `mship status` — tells you the task, phase, repos, test results, and blocked state
2. Run `mship log` — tells you the narrative of what you were doing
3. Run `mship log --last 3` — just the recent entries if the log is long

**Always log your progress** before ending a session or when you've completed a significant step. Future you (or another agent) will thank you.

## Integration with Superpowers

This skill is additive — it coordinates superpowers skills, not replaces them.

**Before brainstorming:** `mship spawn "description"` → `mship phase plan`
**Before implementing:** `mship phase dev` → use TDD skill
**Before reviewing:** `mship test` → `mship phase review` → use code-review skill
**Before finishing:** `mship finish` → creates PRs with merge order

## Single-Repo vs Multi-Repo

Everything works the same. In a single-repo workspace:
- `mship spawn` creates one worktree
- `mship test` runs tests in one repo
- `mship finish` creates one PR (no coordination block needed)
- Phases, logging, and blocked state work identically

## What NOT to Do

- **Don't skip phases** — follow plan → dev → review → run
- **Don't create worktrees manually** — use `mship spawn`
- **Don't forget to log** — `mship log "what I did"` after significant work
- **Don't merge PRs out of order** — follow the merge order in the PR coordination block
- **Don't ignore soft gate warnings** — they exist for a reason
- **Don't run `mship finish` without passing tests** — run `mship test` first
```

- [ ] **Step 3: Commit**

```bash
git add skills/working-with-mothership/SKILL.md
git commit -m "feat: add working-with-mothership superpowers skill"
```

---

### Task 5: Integration Test

**Files:**
- Create: `tests/test_finish_integration.py`

- [ ] **Step 1: Write the integration test**

`tests/test_finish_integration.py`:
```python
"""Integration test: spawn → finish creates PRs with coordination blocks."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def finish_workspace(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    # Mock shell for spawn (setup tasks)
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)

    yield workspace_with_git, mock_shell
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_finish_single_repo_no_coordination_block(finish_workspace):
    workspace, mock_shell = finish_workspace

    # Spawn single repo task
    result = runner.invoke(app, ["spawn", "single repo test", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    # Set up mock for finish: gh auth, git push, gh pr create
    pr_url = "https://github.com/org/shared/pull/99"
    call_log = []

    def mock_run(cmd, cwd, env=None):
        call_log.append(cmd)
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout=f"{pr_url}\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    # Verify PR URL stored in state
    mgr = StateManager(workspace / ".mothership")
    state = mgr.load()
    assert state.tasks["single-repo-test"].pr_urls["shared"] == pr_url

    # Single repo: no gh pr edit calls (no coordination block)
    edit_calls = [c for c in call_log if "gh pr edit" in c]
    assert len(edit_calls) == 0


def test_finish_multi_repo_adds_coordination(finish_workspace):
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "multi repo test", "--repos", "shared,auth-service"])
    assert result.exit_code == 0, result.output

    pr_counter = [0]
    call_log = []

    def mock_run(cmd, cwd, env=None):
        call_log.append(cmd)
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            pr_counter[0] += 1
            return ShellResult(returncode=0, stdout=f"https://github.com/org/repo/pull/{pr_counter[0]}\n", stderr="")
        if "gh pr view" in cmd:
            return ShellResult(returncode=0, stdout="original body", stderr="")
        if "gh pr edit" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    # Verify 2 PRs created
    create_calls = [c for c in call_log if "gh pr create" in c]
    assert len(create_calls) == 2

    # Verify coordination blocks added (gh pr edit called for each)
    edit_calls = [c for c in call_log if "gh pr edit" in c]
    assert len(edit_calls) == 2


def test_finish_idempotent_rerun(finish_workspace):
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "idempotent test", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    # First finish
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=0, stdout="Logged in", stderr="") if "gh auth" in cmd
        else ShellResult(returncode=0, stdout="", stderr="") if "git push" in cmd
        else ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="") if "gh pr create" in cmd
        else ShellResult(returncode=0, stdout="", stderr="")
    )

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    # Second finish — should skip existing PR
    call_log = []
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        call_log.append(cmd) or
        (ShellResult(returncode=0, stdout="Logged in", stderr="") if "gh auth" in cmd
        else ShellResult(returncode=0, stdout="", stderr=""))
    )

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    # No gh pr create on second run
    create_calls = [c for c in call_log if c and "gh pr create" in c]
    assert len(create_calls) == 0
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_finish_integration.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 3: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_finish_integration.py
git commit -m "test: add integration tests for mship finish PR creation"
```

---

## Self-Review

**Spec coverage:**
- PR creation via gh CLI: Task 2 (PRManager), Task 3 (CLI wiring)
- Coordination block with cross-references: Task 2 (`build_coordination_block`), Task 3 (update PRs)
- Single-repo: no coordination block: Task 2 (returns empty string for 1 PR)
- PR URLs in state: Task 1 (`pr_urls` field)
- Idempotent re-run: Task 3 (skips existing `pr_urls`)
- Error handling (gh missing, auth, push fail): Task 2 + Task 3
- Handoff flag unchanged: Task 3 (preserved from existing code)
- Post-PR worktrees remain: Task 3 (no cleanup after PR creation)
- Auto-log PR URLs: Task 3 (logs each PR URL)
- Superpowers skill: Task 4 (complete SKILL.md)
- Integration tests: Task 5

**Placeholder scan:** No TBDs or TODOs.

**Type consistency:** `PRManager.create_pr` returns `str` (URL). `task.pr_urls` is `dict[str, str]` (repo→URL). `pr_list` is `list[dict]` with keys `repo`, `url`, `order` — consistent across Task 3 CLI and Task 2's `build_coordination_block`.
