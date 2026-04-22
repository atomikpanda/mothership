# `mship commit` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `mship commit "<msg>"` — iterates `task.affected_repos`, commits staged changes in each worktree that has them, pushes post-finish if a PR exists, and journals one entry per repo.

**Architecture:** Single new `src/mship/cli/commit.py` file registers one command. Reuses `resolve_for_command` for task lookup + breadcrumb, `container.shell()` for git operations, and `container.log_manager()` for journal entries.

**Tech Stack:** Python 3.14, typer, pytest, existing `ShellRunner` + `LogManager` in the container.

**Reference spec:** `docs/superpowers/specs/2026-04-22-mship-commit-design.md`

---

## File structure

**New files:**
- `src/mship/cli/commit.py` — one command, ~90 lines.
- `tests/cli/test_commit.py` — 8 integration tests.

**Modified files:**
- `src/mship/cli/__init__.py` — register the new module.
- `src/mship/skills/working-with-mothership/SKILL.md` — name `mship commit` as the sanctioned post-finish iteration tool.

**Task ordering:** Task 1 ships the command + registration + all tests. Task 2 updates the skill doc (can't land without the command existing). Task 3 is smoke + PR.

---

## Task 1: `mship commit` command + tests

**Files:**
- Create: `src/mship/cli/commit.py`
- Create: `tests/cli/test_commit.py`
- Modify: `src/mship/cli/__init__.py` — register the module.

**Context:** The command iterates `task.affected_repos`, skips repos with no staged changes, commits + pushes (post-finish) + journals for the rest. Hard-errors if nothing is staged anywhere. Follows existing CLI patterns (see `src/mship/cli/block.py` for a close analog).

- [ ] **Step 1.1: Write failing integration tests**

Create `tests/cli/test_commit.py`:

```python
"""Integration tests for `mship commit`. See #29."""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


def _set_finished(workspace: Path, slug: str, pr_urls: dict[str, str]) -> None:
    """Mark a spawned task as finished with given pr_urls."""
    import yaml
    from datetime import datetime, timezone
    state_path = workspace / ".mothership" / "state.yaml"
    data = yaml.safe_load(state_path.read_text())
    data["tasks"][slug]["finished_at"] = datetime.now(timezone.utc).isoformat()
    data["tasks"][slug]["pr_urls"] = pr_urls
    state_path.write_text(yaml.safe_dump(data))


def test_commit_pre_finish_single_repo(configured_git_app: Path):
    """Stage in one worktree pre-finish → commit + journal, no push."""
    runner.invoke(app, ["spawn", "pre finish commit", "--repos", "shared"])
    slug = "pre-finish-commit"

    commits: list[str] = []
    pushes: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            # Staged in shared's worktree; not staged elsewhere.
            if "shared" in str(cwd):
                return ShellResult(returncode=1, stdout="", stderr="")  # staged
            return ShellResult(returncode=0, stdout="", stderr="")  # clean
        if "git commit -m" in cmd:
            commits.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="7f3a1b2abcdef\n", stderr="")
        if "git push" in cmd:
            pushes.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: typo", "--task", slug])
        assert result.exit_code == 0, result.output
        assert len(commits) == 1 and "shared" in commits[0]
        assert pushes == []  # pre-finish → no push
        # Journal recorded
        log = (configured_git_app / ".mothership" / "logs" / f"{slug}.md").read_text()
        assert "fix: typo" in log
        assert "repo=shared" in log
        assert "action=committed" in log
    finally:
        container.shell.reset_override()


def test_commit_pre_finish_multi_repo(configured_git_app: Path):
    """Stage in two worktrees → both commit, no push, two journal entries."""
    runner.invoke(app, ["spawn", "multi pre", "--repos", "shared,auth-service"])
    slug = "multi-pre"

    commits: list[str] = []
    pushes: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")  # staged everywhere
        if "git commit -m" in cmd:
            commits.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="abc123def\n", stderr="")
        if "git push" in cmd:
            pushes.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "feat: coordinated", "--task", slug])
        assert result.exit_code == 0, result.output
        assert len(commits) == 2
        assert pushes == []
        log = (configured_git_app / ".mothership" / "logs" / f"{slug}.md").read_text()
        assert log.count("feat: coordinated") == 2
        assert "repo=shared" in log
        assert "repo=auth-service" in log
    finally:
        container.shell.reset_override()


def test_commit_post_finish_single_repo(configured_git_app: Path):
    """Finished task + PR → commit + push + journal."""
    runner.invoke(app, ["spawn", "post single", "--repos", "shared"])
    slug = "post-single"
    _set_finished(configured_git_app, slug, {"shared": "https://github.com/o/r/pull/7"})

    commits: list[str] = []
    pushes: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git commit -m" in cmd:
            commits.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="deadbeef\n", stderr="")
        if "git push" in cmd:
            pushes.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: review", "--task", slug])
        assert result.exit_code == 0, result.output
        assert len(commits) == 1
        assert len(pushes) == 1 and "shared" in pushes[0]
    finally:
        container.shell.reset_override()


def test_commit_post_finish_multi_repo(configured_git_app: Path):
    """Finished multi-repo task → both commit + push."""
    runner.invoke(app, ["spawn", "post multi", "--repos", "shared,auth-service"])
    slug = "post-multi"
    _set_finished(configured_git_app, slug, {
        "shared": "https://github.com/o/r/pull/1",
        "auth-service": "https://github.com/o/r/pull/2",
    })

    commits: list[str] = []
    pushes: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git commit -m" in cmd:
            commits.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="abcdef12\n", stderr="")
        if "git push" in cmd:
            pushes.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: both repos", "--task", slug])
        assert result.exit_code == 0, result.output
        assert len(commits) == 2
        assert len(pushes) == 2
    finally:
        container.shell.reset_override()


def test_commit_skips_repos_without_staged_changes(configured_git_app: Path):
    """Partial staging: one repo staged, one not → only first commits."""
    runner.invoke(app, ["spawn", "partial", "--repos", "shared,auth-service"])
    slug = "partial"

    commits: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            # Only shared has staged changes; auth-service is clean.
            if "shared" in str(cwd):
                return ShellResult(returncode=1, stdout="", stderr="")
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git commit -m" in cmd:
            commits.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="cafe1234\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: just shared", "--task", slug])
        assert result.exit_code == 0, result.output
        assert len(commits) == 1 and "shared" in commits[0]
        # auth-service is mentioned as skipped in output
        assert "auth-service" in result.output
        assert "skipped" in result.output.lower() or "nothing staged" in result.output.lower()
    finally:
        container.shell.reset_override()


def test_commit_errors_when_nothing_staged_anywhere(configured_git_app: Path):
    """No staged changes in any worktree → exit 1 with clear message."""
    runner.invoke(app, ["spawn", "nothing", "--repos", "shared,auth-service"])
    slug = "nothing"

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")  # clean everywhere
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "nope", "--task", slug])
        assert result.exit_code != 0
        assert "nothing staged" in result.output.lower()
        assert "git add" in result.output.lower()
    finally:
        container.shell.reset_override()


def test_commit_git_commit_failure_surfaces(configured_git_app: Path):
    """Hook rejection during git commit → exit 1, error message surfaces stderr."""
    runner.invoke(app, ["spawn", "hook fail", "--repos", "shared"])
    slug = "hook-fail"

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git commit -m" in cmd:
            return ShellResult(
                returncode=1, stdout="",
                stderr="pre-commit hook failed: lint errors",
            )
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "will fail", "--task", slug])
        assert result.exit_code != 0
        assert "shared" in result.output
        assert "pre-commit hook failed" in result.output
    finally:
        container.shell.reset_override()


def test_commit_push_failure_surfaces_post_finish(configured_git_app: Path):
    """Push fails post-finish → exit 1, but journal DOES record the commit."""
    runner.invoke(app, ["spawn", "push fail", "--repos", "shared"])
    slug = "push-fail"
    _set_finished(configured_git_app, slug, {"shared": "https://github.com/o/r/pull/1"})

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git commit -m" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="1a2b3c4\n", stderr="")
        if "git push" in cmd:
            return ShellResult(
                returncode=1, stdout="",
                stderr="! [rejected] feat/branch (non-fast-forward)",
            )
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: will-fail-push", "--task", slug])
        assert result.exit_code != 0
        assert "push" in result.output.lower()
        assert "shared" in result.output
        # Commit happened locally; journal records it.
        log = (configured_git_app / ".mothership" / "logs" / f"{slug}.md").read_text()
        assert "fix: will-fail-push" in log
    finally:
        container.shell.reset_override()
```

- [ ] **Step 1.2: Run tests to verify they fail**

`uv run pytest tests/cli/test_commit.py -v`
Expected: all 8 fail with `No such command 'commit'` (the command isn't registered yet).

- [ ] **Step 1.3: Create `src/mship/cli/commit.py`**

```python
"""`mship commit <msg>` — post-finish patch workflow.

Iterates `task.affected_repos` and commits staged changes in each worktree
that has them. Post-finish: also pushes to the existing PR. Always journals
one entry per repo committed. See #29.
"""
from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def commit(
        message: str = typer.Argument(..., help="Commit message (same for every repo with staged changes)"),
        task: Optional[str] = typer.Option(
            None, "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var.",
        ),
    ):
        """Commit staged changes across task worktrees; push if finished."""
        import shlex

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        resolved = resolve_for_command("commit", state, task, output)
        t = resolved.task

        shell = container.shell()
        log_mgr = container.log_manager()

        results: list[dict] = []
        skipped: list[str] = []

        for repo_name in t.affected_repos:
            wt = t.worktrees.get(repo_name)
            if wt is None:
                skipped.append(repo_name)
                continue
            wt_path = Path(wt)
            if not wt_path.is_dir():
                skipped.append(repo_name)
                continue

            # Staged check: `git diff --cached --quiet` exits 0 if clean, 1 if staged.
            staged_check = shell.run("git diff --cached --quiet", cwd=wt_path)
            if staged_check.returncode == 0:
                skipped.append(repo_name)
                continue

            # Commit.
            commit_r = shell.run(
                f"git commit -m {shlex.quote(message)}", cwd=wt_path,
            )
            if commit_r.returncode != 0:
                output.error(
                    f"{repo_name}: git commit failed — {commit_r.stderr.strip() or 'unknown error'}"
                )
                raise typer.Exit(code=1)

            # Capture commit SHA.
            sha_r = shell.run("git rev-parse HEAD", cwd=wt_path)
            sha = sha_r.stdout.strip() if sha_r.returncode == 0 else ""

            # Journal BEFORE push so the commit is recorded even if push fails.
            log_mgr.append(
                t.slug, message, repo=repo_name, action="committed",
            )

            pushed = False
            pr_url = t.pr_urls.get(repo_name)
            if t.finished_at is not None and pr_url:
                push_r = shell.run("git push", cwd=wt_path)
                if push_r.returncode != 0:
                    output.error(
                        f"{repo_name}: git push failed — {push_r.stderr.strip() or 'unknown error'}"
                    )
                    raise typer.Exit(code=1)
                pushed = True

            results.append({
                "repo": repo_name,
                "commit_sha": sha,
                "pushed": pushed,
                "pr_url": pr_url if pushed else None,
            })

        if not results:
            output.error(
                "nothing staged in any affected repo. "
                "Run `git add <files>` first."
            )
            raise typer.Exit(code=1)

        if output.is_tty:
            for r in results:
                short = r["commit_sha"][:8] if r["commit_sha"] else "(no sha)"
                base = f"  {r['repo']}: committed {short}"
                if r["pushed"]:
                    output.print(base + f" → pushed to {r['pr_url']}")
                else:
                    output.print(base + " (not pushed — task not finished)")
            for s in skipped:
                output.print(f"  {s}: skipped (nothing staged)")
        else:
            import json as _json
            payload = {
                "task": t.slug,
                "repos": [
                    *results,
                    *[{"repo": s, "skipped": "nothing staged"} for s in skipped],
                ],
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            }
            print(_json.dumps(payload, indent=2))
```

- [ ] **Step 1.4: Register the command**

Edit `src/mship/cli/__init__.py`. Add the import near the existing `_block_mod` / `_log_mod` lines (keep alphabetical-ish grouping):

```python
from mship.cli import commit as _commit_mod
```

And add the registration call near the other `_X_mod.register(app, get_container)` lines:

```python
_commit_mod.register(app, get_container)
```

- [ ] **Step 1.5: Run tests to verify they pass**

`uv run pytest tests/cli/test_commit.py -v`
Expected: 8 passed.

- [ ] **Step 1.6: Run broader `tests/cli/` for regressions**

`uv run pytest tests/cli/ -q 2>&1 | tail -3`
Expected: all pass. The new command is purely additive.

- [ ] **Step 1.7: Commit**

```bash
git add src/mship/cli/commit.py src/mship/cli/__init__.py tests/cli/test_commit.py
git commit -m "feat(cli): mship commit for post-finish coordinated commits"
mship journal "#29: mship commit <msg> iterates task.affected_repos, commits staged changes in each, pushes post-finish when PR exists, journals per repo" --action committed
```

---

## Task 2: Update skill doc

**Files:**
- Modify: `src/mship/skills/working-with-mothership/SKILL.md` — name `mship commit` as the sanctioned post-finish iteration tool.

**Context:** The existing skill likely has guidance saying "after `mship finish`, open a new task for reviewer feedback." Replace that with a recommendation to use `mship commit` for small fixes on the same branch. Keep the "open a new task" option for larger changes.

- [ ] **Step 2.1: Find the existing guidance**

Run: `grep -n "after .*finish\|open a new task\|reviewer feedback\|post-finish" src/mship/skills/working-with-mothership/SKILL.md`
If no existing section matches, the guidance isn't there yet and we're adding it fresh. In either case, the goal for this step is to identify the right INSERT or REPLACE location.

- [ ] **Step 2.2: Add or update the post-finish section**

Edit `src/mship/skills/working-with-mothership/SKILL.md`. In the section about the task lifecycle (typically near the `mship finish` / `mship close` discussion), add:

```markdown
### Iterating after `mship finish` (reviewer feedback, CI fixes, typos)

For small post-finish changes — reviewer comments, CI fixes, doc tweaks — use `mship commit <msg>` instead of spawning a new task:

1. Stage the fix with `git add <files>` in the worktree.
2. Run `mship commit "<commit message>"`. This iterates your task's `affected_repos`, commits staged changes in every worktree that has them, pushes to the existing PR (since the task is finished), and appends a journal entry per repo.

For coordinated multi-repo fixes: stage in each worktree you need, then one `mship commit` handles all of them with the same commit message.

For larger changes — new features, significant refactors — spawn a new task via `mship spawn`. Post-finish commits are for small iterations on the same branch.

`mship phase` remains blocked post-finish (you're in review / integration, not re-planning). `mship journal` and `mship test` continue to work.
```

If an existing section covers post-finish behavior, REPLACE its contents with the above rather than duplicating.

- [ ] **Step 2.3: Commit**

```bash
git add src/mship/skills/working-with-mothership/SKILL.md
git commit -m "docs(skill): name mship commit as post-finish iteration tool"
mship journal "#29: skill doc now directs agents to `mship commit <msg>` for post-finish PR iterations instead of spawning a new task" --action committed
```

---

## Task 3: Smoke + PR

**Files:**
- None (verification + PR only).

- [ ] **Step 3.1: Reinstall**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/post-finish-patch
uv tool install --reinstall --from . mothership
```

- [ ] **Step 3.2: Smoke the pre-finish path**

Inside the task worktree, stage something trivial + commit:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/post-finish-patch
echo "# smoke" > SMOKE.md
git add SMOKE.md
mship commit "smoke: pre-finish commit test" 2>&1 | head -10
rm SMOKE.md  # undo the smoke marker
git reset HEAD~1 --soft && git reset
```

Expected output includes `→ task: post-finish-patch (resolved via cwd)` breadcrumb and `mothership: committed <sha> (not pushed — task not finished)`.

- [ ] **Step 3.3: Smoke the nothing-staged error**

```bash
mship commit "should fail" 2>&1 | head -3
```

Expected: `ERROR: nothing staged in any affected repo. Run 'git add <files>' first.`

- [ ] **Step 3.4: Full pytest**

`uv run pytest tests/ --ignore=tests/core/view/test_web_port.py 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 3.5: Open the PR**

Write `/tmp/mship-commit-body.md`:

```markdown
## Summary

Closes #29.

Adds `mship commit "<msg>"` for the post-finish patch workflow. Commits staged changes across `task.affected_repos`, pushes to existing PRs when the task is finished, and journals one entry per repo committed.

### Command

```bash
$ git add <files>          # stage in one or more worktrees
$ mship commit "fix: address reviewer feedback"
→ task: xyz  (resolved via cwd)
  shared: committed 7f3a1b2 → pushed to https://github.com/o/r/pull/42
  api-gateway: committed e9c2d8f → pushed to https://github.com/o/r/pull/43
```

Pre-finish: commits locally, doesn't push (no PR yet). Post-finish: commits + pushes. Always journals.

### Design rules

- Staging IS the selection mechanism (no multi-repo flag needed).
- Same message applies to every repo committed in one invocation (use separate invocations for different messages).
- No `--amend`, no `--no-verify`, no auto-stage. Respects pre-commit hooks.
- Errors hard when nothing is staged in any repo.

### Skill doc

`src/mship/skills/working-with-mothership/SKILL.md` now names `mship commit` as the sanctioned post-finish iteration tool (replacing "open a new task" guidance for small fixes).

## Test plan

- [x] `tests/cli/test_commit.py`: 8 integration tests — pre-finish single/multi-repo, post-finish single/multi-repo, partial skip, nothing-staged error, commit failure, push-failure-post-finish.
- [x] Full suite green.
- [x] Manual smoke: pre-finish commit + nothing-staged error behave as expected.

## Anti-goals preserved

- No `--amend` / `--no-verify` / auto-stage / pre-finish push.
- Existing `mship finish --force` behavior unchanged (re-pushes existing commits; orthogonal to `mship commit` which creates new commits).
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/post-finish-patch
mship finish --body-file /tmp/mship-commit-body.md --title "feat(mship): commit command for post-finish iteration (#29)"
```

Expected: PR URL returned.

---

## Done when

- [x] `mship commit "<msg>"` is registered and callable.
- [x] Iterates `task.affected_repos`; commits where staged; skips where not.
- [x] Post-finish + PR present → pushes after commit.
- [x] One journal entry per repo committed.
- [x] Hard-errors when nothing staged in any repo.
- [x] 8 integration tests pass.
- [x] Skill doc updated.
- [x] Full pytest green.
- [x] Manual smoke confirms pre-finish commit + nothing-staged error.
