# mship dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-17-mship-dispatch-design.md`

**Goal:** Ship `mship dispatch` — an agent-agnostic primitive that emits a self-contained markdown subagent-prompt to stdout for a resolved mship task.

**Architecture:** Pure builder in `src/mship/core/dispatch.py` (no I/O, trivially unit-testable). Thin Typer CLI in `src/mship/cli/dispatch.py` that resolves task + repo, gathers inputs (journal entries, base-SHA info, skills source, AGENTS.md path), calls the builder, prints the result.

**Tech Stack:** Python 3.14, Typer, subprocess-wrapped `git`, pytest.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `src/mship/core/dispatch.py` | Pure builder: `BaseShaInfo`, `SkillRef`, `canonical_skills()`, `resolve_repo()`, `collect_base_sha_info()`, `build_dispatch_prompt()` | create |
| `tests/core/test_dispatch.py` | Unit tests for all pure helpers + builder substring checks | create |
| `src/mship/cli/dispatch.py` | Thin Typer wrapper — task/repo resolve, gather, call builder, print | create |
| `tests/cli/test_dispatch.py` | CliRunner integration tests covering resolution paths | create |
| `src/mship/cli/__init__.py` | Register the new dispatch command | modify |

---

## Task 1: Pure helpers — dataclasses, canonical_skills, resolve_repo (TDD)

**Files:**
- Create: `src/mship/core/dispatch.py`
- Create: `tests/core/test_dispatch.py`

- [ ] **Step 1.1: Write failing test for `canonical_skills()`**

Create `tests/core/test_dispatch.py`:

```python
"""Unit tests for src/mship/core/dispatch.py."""
from __future__ import annotations

from pathlib import Path

from mship.core.dispatch import SkillRef, canonical_skills


def test_canonical_skills_returns_expected_four_in_order():
    src = Path("/fake/pkg/skills")
    refs = canonical_skills(src)
    assert [r.name for r in refs] == [
        "working-with-mothership",
        "test-driven-development",
        "finishing-a-development-branch",
        "verification-before-completion",
    ]
    for r in refs:
        assert isinstance(r, SkillRef)
        assert r.path == src / r.name / "SKILL.md"
```

Run: `uv run pytest tests/core/test_dispatch.py::test_canonical_skills_returns_expected_four_in_order -v`
Expected: FAIL — `mship.core.dispatch` doesn't exist.

- [ ] **Step 1.2: Implement `SkillRef` + `canonical_skills()`**

Create `src/mship/core/dispatch.py`:

```python
"""Build the agent-agnostic subagent-prompt emitted by `mship dispatch`.

Pure builder — zero I/O, trivially unit-testable. The CLI wrapper in
src/mship/cli/dispatch.py handles resolution, subprocess calls, and stdout.

See docs/superpowers/specs/2026-04-17-mship-dispatch-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


_CANONICAL_SKILL_NAMES: tuple[str, ...] = (
    "working-with-mothership",
    "test-driven-development",
    "finishing-a-development-branch",
    "verification-before-completion",
)


@dataclass(frozen=True)
class SkillRef:
    name: str
    path: Path


def canonical_skills(pkg_skills_source: Path) -> list[SkillRef]:
    """Return the four canonical skills every dispatched subagent should read."""
    return [
        SkillRef(name=n, path=pkg_skills_source / n / "SKILL.md")
        for n in _CANONICAL_SKILL_NAMES
    ]
```

Run: `uv run pytest tests/core/test_dispatch.py::test_canonical_skills_returns_expected_four_in_order -v`
Expected: PASS

- [ ] **Step 1.3: Write failing tests for `resolve_repo()`**

Append to `tests/core/test_dispatch.py`:

```python
from datetime import datetime, timezone

import pytest

from mship.core.dispatch import resolve_repo
from mship.core.state import Task


def _task(worktrees: dict[str, Path], active_repo: str | None = None) -> Task:
    return Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=list(worktrees.keys()),
        worktrees=worktrees, branch="feat/t",
        active_repo=active_repo,
    )


def test_resolve_repo_flag_wins(tmp_path: Path):
    t = _task({"a": tmp_path / "a", "b": tmp_path / "b"}, active_repo="a")
    assert resolve_repo(t, repo_flag="b") == "b"


def test_resolve_repo_falls_back_to_active_repo(tmp_path: Path):
    t = _task({"a": tmp_path / "a", "b": tmp_path / "b"}, active_repo="b")
    assert resolve_repo(t, repo_flag=None) == "b"


def test_resolve_repo_uses_sole_worktree_when_unambiguous(tmp_path: Path):
    t = _task({"only": tmp_path / "only"})
    assert resolve_repo(t, repo_flag=None) == "only"


def test_resolve_repo_errors_when_multiple_and_unambiguous(tmp_path: Path):
    t = _task({"a": tmp_path / "a", "b": tmp_path / "b"})
    with pytest.raises(ValueError, match="affects 2 repos"):
        resolve_repo(t, repo_flag=None)


def test_resolve_repo_errors_on_unknown_flag(tmp_path: Path):
    t = _task({"a": tmp_path / "a"})
    with pytest.raises(ValueError, match="unknown repo"):
        resolve_repo(t, repo_flag="nope")
```

Run: `uv run pytest tests/core/test_dispatch.py -k resolve_repo -v`
Expected: 5 FAIL — `resolve_repo` not defined.

- [ ] **Step 1.4: Implement `resolve_repo()`**

Append to `src/mship/core/dispatch.py`:

```python
from mship.core.state import Task


def resolve_repo(task: Task, repo_flag: str | None) -> str:
    """Pick which repo's worktree the dispatch prompt targets.

    Priority: --repo flag > task.active_repo > sole worktree > ValueError.
    """
    if repo_flag is not None:
        if repo_flag not in task.worktrees:
            raise ValueError(
                f"unknown repo: {repo_flag!r}. "
                f"Task affects: {sorted(task.worktrees)}"
            )
        return repo_flag
    if task.active_repo and task.active_repo in task.worktrees:
        return task.active_repo
    if len(task.worktrees) == 1:
        return next(iter(task.worktrees))
    raise ValueError(
        f"task {task.slug!r} affects {len(task.worktrees)} repos and no "
        f"active_repo is set; pass --repo <name> or run mship switch <repo> "
        f"first. Affected repos: {sorted(task.worktrees)}"
    )
```

Run: `uv run pytest tests/core/test_dispatch.py -k resolve_repo -v`
Expected: 5 PASS

- [ ] **Step 1.5: Commit**

```bash
git add src/mship/core/dispatch.py tests/core/test_dispatch.py
git commit -m "feat(dispatch): core helpers — canonical_skills + resolve_repo"
```

---

## Task 2: collect_base_sha_info (TDD, real git fixture)

**Files:**
- Modify: `src/mship/core/dispatch.py`
- Modify: `tests/core/test_dispatch.py`

- [ ] **Step 2.1: Write failing tests for `BaseShaInfo` + `collect_base_sha_info`**

Append to `tests/core/test_dispatch.py`:

```python
import os
import subprocess

from mship.core.dispatch import BaseShaInfo, collect_base_sha_info


def _git(args: list[str], cwd: Path, env_extra: dict | None = None):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if env_extra:
        env.update(env_extra)
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _dispatch_git_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare origin + working clone with one initial commit on main."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)],
                   check=True, capture_output=True)
    (clone / "README.md").write_text("init\n")
    _git(["add", "."], cwd=clone)
    _git(["commit", "-qm", "init"], cwd=clone)
    _git(["push", "-q", "origin", "main"], cwd=clone)
    return origin, clone


def test_base_sha_info_clean_state(tmp_path: Path):
    _, clone = _dispatch_git_fixture(tmp_path)
    info = collect_base_sha_info(clone, base_branch="main")
    assert isinstance(info, BaseShaInfo)
    assert info.base_sha == info.origin_base_sha == info.head_sha
    assert "in sync" in info.summary
    assert info.has_upstream is True


def test_base_sha_info_ahead(tmp_path: Path):
    _, clone = _dispatch_git_fixture(tmp_path)
    (clone / "x.txt").write_text("x\n")
    _git(["add", "."], cwd=clone)
    _git(["commit", "-qm", "x"], cwd=clone)
    info = collect_base_sha_info(clone, base_branch="main")
    assert "1 commit ahead" in info.summary
    assert info.head_sha != info.base_sha


def test_base_sha_info_no_upstream(tmp_path: Path):
    _, clone = _dispatch_git_fixture(tmp_path)
    # Drop the remote so origin/main lookup fails
    _git(["remote", "remove", "origin"], cwd=clone)
    info = collect_base_sha_info(clone, base_branch="main")
    assert info.has_upstream is False
    assert "no upstream" in info.summary
    assert info.origin_base_sha is None
```

Run: `uv run pytest tests/core/test_dispatch.py -k base_sha -v`
Expected: 3 FAIL — `BaseShaInfo`/`collect_base_sha_info` not defined.

- [ ] **Step 2.2: Implement `BaseShaInfo` + `collect_base_sha_info()`**

Append to `src/mship/core/dispatch.py`:

```python
import subprocess


@dataclass(frozen=True)
class BaseShaInfo:
    base_sha: str | None         # local <base_branch>
    origin_base_sha: str | None  # remote origin/<base_branch>
    head_sha: str                # current HEAD of the worktree
    ahead_of_base: int | None
    base_behind_origin: int | None
    has_upstream: bool
    summary: str                 # one-line human-readable


def _git_out(args: list[str], cwd: Path, timeout: int = 10) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def collect_base_sha_info(worktree: Path, base_branch: str) -> BaseShaInfo:
    """Probe local `<base>`, `origin/<base>`, and HEAD. Graceful on missing upstream."""
    head_sha = _git_out(["rev-parse", "--short", "HEAD"], cwd=worktree) or "?"
    base_sha = _git_out(["rev-parse", "--short", base_branch], cwd=worktree)
    origin_base_sha = _git_out(
        ["rev-parse", "--short", f"origin/{base_branch}"], cwd=worktree,
    )
    has_upstream = origin_base_sha is not None

    ahead_of_base: int | None = None
    base_behind_origin: int | None = None
    if base_sha:
        out = _git_out(["rev-list", "--count", f"{base_branch}..HEAD"], cwd=worktree)
        try:
            ahead_of_base = int(out) if out is not None else None
        except ValueError:
            ahead_of_base = None
    if base_sha and has_upstream:
        out = _git_out(
            ["rev-list", "--count", f"{base_branch}..origin/{base_branch}"],
            cwd=worktree,
        )
        try:
            base_behind_origin = int(out) if out is not None else None
        except ValueError:
            base_behind_origin = None

    summary = _summarize_base_sha(
        ahead_of_base=ahead_of_base,
        base_behind_origin=base_behind_origin,
        has_upstream=has_upstream,
        base_branch=base_branch,
    )
    return BaseShaInfo(
        base_sha=base_sha, origin_base_sha=origin_base_sha, head_sha=head_sha,
        ahead_of_base=ahead_of_base, base_behind_origin=base_behind_origin,
        has_upstream=has_upstream, summary=summary,
    )


def _summarize_base_sha(
    *, ahead_of_base: int | None, base_behind_origin: int | None,
    has_upstream: bool, base_branch: str,
) -> str:
    parts = []
    if not has_upstream:
        parts.append(f"no upstream tracked for `{base_branch}`")
    elif base_behind_origin == 0:
        parts.append(f"base is in sync with origin")
    elif base_behind_origin and base_behind_origin > 0:
        plural = "s" if base_behind_origin != 1 else ""
        parts.append(f"base is {base_behind_origin} commit{plural} behind origin")
    if ahead_of_base is not None:
        plural = "s" if ahead_of_base != 1 else ""
        if ahead_of_base == 0:
            parts.append(f"HEAD is at base")
        else:
            parts.append(f"HEAD is {ahead_of_base} commit{plural} ahead of base")
    return "; ".join(parts) if parts else "unknown"
```

Run: `uv run pytest tests/core/test_dispatch.py -k base_sha -v`
Expected: 3 PASS

- [ ] **Step 2.3: Commit**

```bash
git add src/mship/core/dispatch.py tests/core/test_dispatch.py
git commit -m "feat(dispatch): collect_base_sha_info with graceful no-upstream"
```

---

## Task 3: build_dispatch_prompt (TDD, substring assertions)

**Files:**
- Modify: `src/mship/core/dispatch.py`
- Modify: `tests/core/test_dispatch.py`

- [ ] **Step 3.1: Write failing tests for `build_dispatch_prompt`**

Append to `tests/core/test_dispatch.py`:

```python
from mship.core.dispatch import build_dispatch_prompt
from mship.core.log import LogEntry


def _info_clean() -> BaseShaInfo:
    return BaseShaInfo(
        base_sha="abc1234", origin_base_sha="abc1234", head_sha="def5678",
        ahead_of_base=3, base_behind_origin=0, has_upstream=True,
        summary="base is in sync with origin; HEAD is 3 commits ahead of base",
    )


def test_build_prompt_contains_worktree_path_cd_directive(tmp_path: Path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    task = _task({"repo": worktree})
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="do X",
        journal_entries=[], base_sha_info=_info_clean(),
        agents_md_path=tmp_path / "AGENTS.md",
        pkg_skills_source=tmp_path / "skills",
    )
    assert f"cd {worktree}" in out
    assert "Work from" in out
    assert "pre-commit hook will refuse" in out


def test_build_prompt_embeds_instruction_verbatim(tmp_path: Path):
    task = _task({"repo": tmp_path / "wt"})
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="implement the --title flag from #45",
        journal_entries=[], base_sha_info=_info_clean(),
        agents_md_path=None, pkg_skills_source=tmp_path / "skills",
    )
    assert "> implement the --title flag from #45" in out


def test_build_prompt_contains_task_facts(tmp_path: Path):
    task = Task(
        slug="my-task", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["repo"],
        worktrees={"repo": tmp_path / "wt"},
        branch="feat/my-task", base_branch="main", active_repo="repo",
    )
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="x",
        journal_entries=[], base_sha_info=_info_clean(),
        agents_md_path=None, pkg_skills_source=tmp_path / "skills",
    )
    assert "my-task" in out
    assert "feat/my-task" in out
    assert "main" in out  # base_branch
    assert "active repo" in out.lower()


def test_build_prompt_contains_base_sha_block(tmp_path: Path):
    task = _task({"repo": tmp_path / "wt"})
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="x",
        journal_entries=[], base_sha_info=_info_clean(),
        agents_md_path=None, pkg_skills_source=tmp_path / "skills",
    )
    assert "abc1234" in out
    assert "def5678" in out
    assert "3 commits ahead" in out


def test_build_prompt_journal_empty_state_when_no_entries(tmp_path: Path):
    task = _task({"repo": tmp_path / "wt"})
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="x",
        journal_entries=[], base_sha_info=_info_clean(),
        agents_md_path=None, pkg_skills_source=tmp_path / "skills",
    )
    assert "No entries yet" in out


def test_build_prompt_journal_renders_bulleted_list(tmp_path: Path):
    task = _task({"repo": tmp_path / "wt"})
    entries = [
        LogEntry(
            timestamp=datetime(2026, 4, 17, 18, 0, tzinfo=timezone.utc),
            message="first commit done", action="committed",
        ),
        LogEntry(
            timestamp=datetime(2026, 4, 17, 18, 10, tzinfo=timezone.utc),
            message="tests green", action="ran tests", test_state="pass",
        ),
    ]
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="x",
        journal_entries=entries, base_sha_info=_info_clean(),
        agents_md_path=None, pkg_skills_source=tmp_path / "skills",
    )
    assert "first commit done" in out
    assert "tests green" in out
    assert "2026-04-17T18:00:00" in out


def test_build_prompt_contains_three_convention_bullets(tmp_path: Path):
    task = _task({"repo": tmp_path / "wt"})
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="x",
        journal_entries=[], base_sha_info=_info_clean(),
        agents_md_path=None, pkg_skills_source=tmp_path / "skills",
    )
    assert "mship finish --body-file" in out
    assert "main checkout" in out
    assert "--bypass-" in out


def test_build_prompt_lists_canonical_skills_with_paths(tmp_path: Path):
    task = _task({"repo": tmp_path / "wt"})
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="x",
        journal_entries=[], base_sha_info=_info_clean(),
        agents_md_path=None, pkg_skills_source=tmp_path / "skills",
    )
    for name in [
        "working-with-mothership", "test-driven-development",
        "finishing-a-development-branch", "verification-before-completion",
    ]:
        assert name in out
        assert f"{tmp_path / 'skills' / name / 'SKILL.md'}" in out


def test_build_prompt_includes_agents_md_path_when_present(tmp_path: Path):
    task = _task({"repo": tmp_path / "wt"})
    agents = tmp_path / "AGENTS.md"
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="x",
        journal_entries=[], base_sha_info=_info_clean(),
        agents_md_path=agents, pkg_skills_source=tmp_path / "skills",
    )
    assert str(agents) in out


def test_build_prompt_omits_agents_md_line_when_absent(tmp_path: Path):
    task = _task({"repo": tmp_path / "wt"})
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="x",
        journal_entries=[], base_sha_info=_info_clean(),
        agents_md_path=None, pkg_skills_source=tmp_path / "skills",
    )
    assert "Full doc:" not in out


def test_build_prompt_contains_finish_contract(tmp_path: Path):
    task = _task({"repo": tmp_path / "wt"})
    out = build_dispatch_prompt(
        task=task, repo="repo", instruction="x",
        journal_entries=[], base_sha_info=_info_clean(),
        agents_md_path=None, pkg_skills_source=tmp_path / "skills",
    )
    assert "How to finish" in out
    assert "mship test" in out
    assert "--body-file" in out
    assert "PR URL" in out
```

Run: `uv run pytest tests/core/test_dispatch.py -k build_prompt -v`
Expected: 11 FAIL — `build_dispatch_prompt` not defined.

- [ ] **Step 3.2: Implement `build_dispatch_prompt()`**

Append to `src/mship/core/dispatch.py`:

```python
from mship.core.log import LogEntry


_CONVENTIONS_RECAP = """\
These are strictly enforced in this workspace:

- **Use `mship finish --body-file <path>` to open the PR.** Empty bodies are rejected by design. Write a real Summary and Test plan.
- **Don't edit from the main checkout.** Only the worktree path above. The pre-commit hook refuses otherwise.
- **Prefer `--bypass-<check>` over `--force-<check>`** on any mship command that takes one (e.g., `--bypass-reconcile`, `--bypass-audit`). Different flag name if you see `--force-<something>` in older docs; the bypass form is canonical.
"""


_FINISH_CONTRACT = """\
When the work is done:

1. Run `mship test` until green (or confirm no test suite applies).
2. Write a PR body as a file — Summary + Test plan.
3. Run `mship finish --body-file <path>` in the worktree.
4. Return the PR URL in your final message.

If you get stuck or find the task is wrong-shaped, stop and report back with what you tried and where you're blocked. Don't guess.
"""


def _render_base_sha_block(info: BaseShaInfo, base_branch: str) -> str:
    origin_val = info.origin_base_sha if info.has_upstream else "(no upstream)"
    return (
        "```\n"
        f"base ({base_branch})  @ {info.base_sha or '?'}\n"
        f"origin/{base_branch}  @ {origin_val}\n"
        f"HEAD                 @ {info.head_sha}    ({info.summary})\n"
        "```"
    )


def _render_journal(entries: list[LogEntry]) -> str:
    if not entries:
        return "*No entries yet — this task hasn't logged anything; your instruction above is the whole picture.*"
    lines = []
    for e in entries:
        ts = e.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        meta_parts = []
        if e.iteration is not None:
            meta_parts.append(f"iter={e.iteration}")
        if e.test_state:
            meta_parts.append(f"test={e.test_state}")
        if e.action:
            meta_parts.append(f'action="{e.action}"')
        meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
        msg = e.message.splitlines()[0] if e.message else ""
        lines.append(f"- **{ts}**{meta} — {msg}")
    return "\n".join(lines)


def _render_skills(skills: list[SkillRef]) -> str:
    return "\n".join(f"- `{s.name}` — `{s.path}`" for s in skills)


def build_dispatch_prompt(
    task: Task,
    repo: str,
    instruction: str,
    *,
    journal_entries: list[LogEntry],
    base_sha_info: BaseShaInfo,
    agents_md_path: Path | None,
    pkg_skills_source: Path,
) -> str:
    """Return the full markdown dispatch prompt for a fresh subagent."""
    worktree = task.worktrees[repo]
    base_branch = task.base_branch or "main"
    skills_block = _render_skills(canonical_skills(pkg_skills_source))
    journal_block = _render_journal(journal_entries)
    base_block = _render_base_sha_block(base_sha_info, base_branch)
    agents_line = f"\nFull doc: `{agents_md_path}`." if agents_md_path else ""

    return f"""\
# Task: {task.slug}

You are a subagent dispatched to work on an in-progress mothership task.

## Work from (mandatory)

Before editing anything: `cd {worktree}`

This is a git worktree checked out on branch `{task.branch}`. Every edit, test run, and commit happens inside this directory. Do not edit from the main checkout — the mship pre-commit hook will refuse and you'll waste a cycle.

## Your instruction

> {instruction}

## Task facts

- **slug:** {task.slug}
- **branch:** {task.branch}
- **base branch:** {base_branch}
- **active repo:** {repo}

## Where the branch stands

{base_block}

## Recent journal (last 10 entries)

{journal_block}

## Conventions (recap)

{_CONVENTIONS_RECAP}{agents_line}

## Read these skills before starting

Invoke via your platform's skill tool if it has one. Direct read paths (always valid; skills ship with mship):

{skills_block}

## How to finish

{_FINISH_CONTRACT}"""
```

Run: `uv run pytest tests/core/test_dispatch.py -k build_prompt -v`
Expected: 11 PASS

- [ ] **Step 3.3: Commit**

```bash
git add src/mship/core/dispatch.py tests/core/test_dispatch.py
git commit -m "feat(dispatch): build_dispatch_prompt markdown emitter"
```

---

## Task 4: CLI wrapper + registration

**Files:**
- Create: `src/mship/cli/dispatch.py`
- Modify: `src/mship/cli/__init__.py`
- Create: `tests/cli/test_dispatch.py`

- [ ] **Step 4.1: Write failing CLI tests**

Create `tests/cli/test_dispatch.py`:

```python
"""Tests for `mship dispatch` CLI."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


runner = CliRunner()


def _bootstrap(tmp_path: Path, worktrees: dict[str, Path], active_repo: str | None = None) -> tuple[Path, Path]:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=list(worktrees.keys()),
        worktrees=worktrees, branch="feat/t",
        base_branch="main", active_repo=active_repo,
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
    container.log_manager.reset()


def test_dispatch_single_repo_task_prints_prompt(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "do the thing"])
        assert result.exit_code == 0, result.output
        assert f"cd {wt}" in result.output
        assert "> do the thing" in result.output
        assert "slug:** t" in result.output or "slug: t" in result.output
    finally:
        _reset()


def test_dispatch_multi_repo_no_active_errors(tmp_path: Path):
    cfg, state_dir = _bootstrap(tmp_path, {
        "a": tmp_path / "a", "b": tmp_path / "b",
    })
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "x"])
        assert result.exit_code == 1
        assert "affects 2 repos" in result.output
    finally:
        _reset()


def test_dispatch_multi_repo_with_repo_flag_picks_that_one(tmp_path: Path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"a": a, "b": b})
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--repo", "b", "-i", "x"])
        assert result.exit_code == 0, result.output
        assert f"cd {b}" in result.output
        assert f"cd {a}" not in result.output
    finally:
        _reset()


def test_dispatch_unknown_repo_errors(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--repo", "nope", "-i", "x"])
        assert result.exit_code == 1
        assert "unknown repo" in result.output
    finally:
        _reset()


def test_dispatch_unknown_task_errors(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "missing", "-i", "x"])
        assert result.exit_code == 1
        assert "Unknown task" in result.output
    finally:
        _reset()
```

Run: `uv run pytest tests/cli/test_dispatch.py -v`
Expected: 5 FAIL — the `dispatch` command isn't registered yet.

- [ ] **Step 4.2: Implement `src/mship/cli/dispatch.py`**

Create `src/mship/cli/dispatch.py`:

```python
"""`mship dispatch` — emit an agent-agnostic subagent prompt to stdout.

See docs/superpowers/specs/2026-04-17-mship-dispatch-design.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_or_exit
from mship.cli.output import Output
from mship.core import dispatch as _d
from mship.core.skill_install import pkg_skills_source


def register(app: typer.Typer, get_container):
    @app.command()
    def dispatch(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug (defaults to cwd-resolved)."),
        repo: Optional[str] = typer.Option(None, "--repo", help="Which repo's worktree to target (multi-repo tasks)."),
        instruction: str = typer.Option(..., "--instruction", "-i", help="Instruction text passed verbatim to the subagent."),
    ):
        """Emit a self-contained markdown subagent prompt to stdout."""
        output = Output()
        container = get_container()
        state = container.state_manager().load()
        task_obj = resolve_or_exit(state, task)

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
            instruction=instruction,
            journal_entries=journal_entries,
            base_sha_info=base_sha_info,
            agents_md_path=agents_md_path,
            pkg_skills_source=pkg_skills_source(),
        )
        # Print directly to stdout (NOT via Output.json — this is meant to be piped).
        print(prompt)
```

- [ ] **Step 4.3: Register the command in `src/mship/cli/__init__.py`**

Find the imports block (around lines 66-83) and add, matching existing style:

```python
from mship.cli import dispatch as _dispatch_mod
```

Find the registration block (near the end of the file) and add:

```python
_dispatch_mod.register(app, get_container)
```

- [ ] **Step 4.4: Run the CLI tests**

Run: `uv run pytest tests/cli/test_dispatch.py -v`
Expected: 5 PASS

- [ ] **Step 4.5: Commit**

```bash
git add src/mship/cli/dispatch.py src/mship/cli/__init__.py tests/cli/test_dispatch.py
git commit -m "feat(dispatch): CLI command emitting prompt to stdout"
```

---

## Task 5: Manual smoke test

**No files modified. Gate before merge.**

- [ ] **Step 5.1: Dispatch from this worktree**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/mship-dispatch-agent-agnostic-subagent-prompt-emitter-52
uv run mship dispatch -i "read the spec at docs/superpowers/specs/2026-04-17-mship-dispatch-design.md and summarize the anti-goals"
```

Expected stdout: the full markdown prompt. Spot-check:
- `cd <worktree absolute path>` line is correct.
- `Your instruction` block contains the verbatim instruction.
- Journal section shows recent entries from this task (spec commit, any subsequent work).
- Base SHA section shows `base (main) @ <sha>`, `origin/main @ <sha>`, `HEAD @ <sha>` with a sensible summary.
- Four canonical skill paths resolve on disk:
  ```bash
  uv run mship dispatch -i test | grep -oE '/[^`]*/SKILL\.md' | xargs ls -l
  ```

---

## Task 6: Final verification + PR

- [ ] **Step 6.1: Full test suite**

```bash
uv run pytest -x -q
```

Expected: all pass (prior total + ~24 new tests).

- [ ] **Step 6.2: Spec coverage check**

Each spec requirement ↔ task:
- ✅ Pure builder, no I/O — Task 1 / 2 / 3
- ✅ CLI wrapper with `--task`/`--repo` override + cwd-resolve default — Task 4
- ✅ `--instruction`/`-i` required — Task 4 (Typer `...` default)
- ✅ Repo resolution order (flag > active_repo > sole > error) — Task 1 (`resolve_repo`)
- ✅ `collect_base_sha_info` with graceful no-upstream — Task 2
- ✅ Canonical four skills — Task 1 (`canonical_skills`)
- ✅ Prompt sections match template — Task 3 tests
- ✅ Journal empty-state handling — Task 3 test
- ✅ AGENTS.md `None` path omits `Full doc:` line — Task 3 test
- ✅ No launcher, no `--json`, no cross-repo single-prompt — not added (anti-goals)
- ✅ Manual smoke validating end-to-end — Task 5

- [ ] **Step 6.3: Open the PR via `mship finish` with a real body**

```bash
cat > /tmp/mship-52-body.md <<'EOF'
## Summary

- New `mship dispatch [--task <slug>] [--repo <name>] -i "<instruction>"` command.
- Emits a self-contained markdown subagent-prompt to stdout (per issue #52).
- Agent-agnostic: no launcher, no JSON, no cross-repo coordination — v1 shape per spec.
- Prompt sections: `cd` directive, instruction verbatim, task facts, base/upstream/HEAD SHAs + human summary, last 10 journal entries, 3-bullet conventions recap + AGENTS.md path, four canonical skill paths, finish contract.
- Pure builder in `src/mship/core/dispatch.py`; CLI wrapper in `src/mship/cli/dispatch.py`.

Closes #52.

## Test plan

- [x] Unit: `canonical_skills`, `resolve_repo` (all four resolution-order cases), `collect_base_sha_info` (clean / ahead / no-upstream), `build_dispatch_prompt` (11 substring assertions covering every section).
- [x] CLI: single-repo happy path, multi-repo with `--repo` override, multi-repo ambiguous error, unknown repo, unknown task.
- [x] Manual smoke: `mship dispatch -i "..."` from this worktree emits a prompt whose `cd` path is correct and whose canonical skill paths resolve on disk.
- [x] Full pytest green.
EOF
mship finish --body-file /tmp/mship-52-body.md
rm /tmp/mship-52-body.md
```

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-04-17-mship-dispatch.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints.

Which approach?
