# Claude Code skill install discoverability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-17-claude-skill-install-discoverability-design.md`

**Goal:** Make mship-bundled skills auto-discoverable by Claude Code without REPL slash commands. Bundle skills into the mship Python package; symlink into each detected agent's discovery dir; add a doctor check.

**Architecture:** Skills move from `skills/` (repo root) into `src/mship/skills/` (package data). A new pure module `src/mship/core/skill_install.py` resolves the source path via `Path(mship.__file__).parent / "skills"` and refreshes per-agent symlinks. The CLI in `src/mship/cli/skill.py` becomes a thin Typer wrapper that imports the core module *as a module* (not by-name) so test monkey-patches at `mship.core.skill_install.X` take effect.

**Tech Stack:** Python 3.14, hatchling build, Typer, pytest, dependency-injector.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `src/mship/skills/<name>/` (16 dirs) | Package-bundled skill content | **moved from `skills/`** |
| `pyproject.toml` | Hatch build config — verify skills ship as package data | modify if needed |
| `src/mship/core/skill_install.py` | Pure helpers: source resolution, owned-target check, symlink refresh, per-agent install | **create** |
| `tests/core/test_skill_install.py` | Unit tests for pure helpers + collision matrix | **create** |
| `src/mship/cli/skill.py` | Thin Typer wrapper invoking `core.skill_install` (imported as module) | rewrite |
| `tests/cli/test_skill.py` | CliRunner tests for new install command | rewrite |
| `src/mship/core/doctor.py` | Skill-availability check appended to `DoctorChecker.run()` | modify |
| `tests/core/test_doctor.py` | Tests for the new skill check | extend |

---

## Task 1: Move skills into the package + verify packaging

**Files:**
- Move: `skills/<name>/` → `src/mship/skills/<name>/` (16 dirs)
- Modify: `pyproject.toml` (only if `uv build` excludes `*.md` — see Step 1.2)
- Test: manual `uv build` inspection

- [ ] **Step 1.1: Move the skills directory**

```bash
git mv skills src/mship/skills
ls src/mship/skills/   # verify 16 entries
```

- [ ] **Step 1.2: Verify hatchling includes markdown in the wheel**

```bash
uv build --wheel
unzip -l dist/mothership-0.1.1-*.whl | grep "src/mship/skills" | head -5
# Expected: SKILL.md files for each skill listed.
# If empty, hatch is excluding *.md — add to pyproject.toml:
#   [tool.hatch.build.targets.wheel.force-include]
#   "src/mship/skills" = "mship/skills"
# Then re-run `uv build --wheel` and re-check.
```

- [ ] **Step 1.3: Sanity-test the runtime path resolves**

```bash
uv run python -c "
from pathlib import Path
import mship
src = Path(mship.__file__).parent / 'skills'
assert src.is_dir(), f'skills dir missing: {src}'
skills = sorted(p.name for p in src.iterdir() if (p / 'SKILL.md').exists())
print(f'{len(skills)} skills: {skills[:3]}...')
"
# Expected: "16 skills: ['brainstorming', 'dispatching-parallel-agents', 'executing-plans']..."
```

- [ ] **Step 1.4: Commit**

```bash
git add src/mship/skills pyproject.toml
git commit -m "refactor(skills): move skills/ into src/mship/skills/ as package data"
```

---

## Task 2: Pure source/owned-target/refresh helpers (TDD)

**Files:**
- Create: `src/mship/core/skill_install.py`
- Test: `tests/core/test_skill_install.py`

- [ ] **Step 2.1: Write failing test for `pkg_skills_source()`**

```python
# tests/core/test_skill_install.py
"""Unit tests for src/mship/core/skill_install.py."""
from __future__ import annotations

from pathlib import Path

import mship
from mship.core.skill_install import pkg_skills_source


def test_pkg_skills_source_resolves_to_package_dir():
    src = pkg_skills_source()
    assert src == Path(mship.__file__).parent / "skills"
    assert src.is_dir()
    assert (src / "working-with-mothership" / "SKILL.md").is_file()
```

Run: `uv run pytest tests/core/test_skill_install.py::test_pkg_skills_source_resolves_to_package_dir -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mship.core.skill_install'`

- [ ] **Step 2.2: Implement `pkg_skills_source()`**

```python
# src/mship/core/skill_install.py
"""Pure helpers for installing mship-bundled skills into agent discovery dirs.

Skills ship as package data under src/mship/skills/. Per-agent installers
symlink them into each agent's discovery dir. CLI in src/mship/cli/skill.py
is a thin wrapper.

Callers MUST import this module as a whole (`from mship.core import
skill_install`) and access functions via the module reference. This lets
tests monkey-patch at `mship.core.skill_install.X` and have the patches
take effect in production callers.

See docs/superpowers/specs/2026-04-17-claude-skill-install-discoverability-design.md.
"""
from __future__ import annotations

from pathlib import Path

import mship


# Historical source paths recognized as "owned by mship" so old symlinks from
# previous install layouts get transparently refreshed instead of safe-skipped.
# Append (never remove) entries when the canonical source path changes.
HISTORICAL_SOURCES: tuple[Path, ...] = (
    Path.home() / ".codex" / "mothership" / "skills",
)


def pkg_skills_source() -> Path:
    """Return the package-bundled skills directory."""
    return Path(mship.__file__).parent / "skills"
```

Run: `uv run pytest tests/core/test_skill_install.py::test_pkg_skills_source_resolves_to_package_dir -v`
Expected: PASS

- [ ] **Step 2.3: Write failing tests for `is_owned_target()`**

```python
# tests/core/test_skill_install.py — append
from mship.core.skill_install import HISTORICAL_SOURCES, is_owned_target, pkg_skills_source


def test_is_owned_target_recognizes_current_pkg_source():
    target_inside_pkg = pkg_skills_source() / "working-with-mothership"
    assert is_owned_target(target_inside_pkg) is True


def test_is_owned_target_recognizes_historical_source():
    historical = HISTORICAL_SOURCES[0]
    target = historical / "foo"
    assert is_owned_target(target) is True


def test_is_owned_target_returns_false_for_foreign_path(tmp_path: Path):
    foreign = tmp_path / "elsewhere" / "skill"
    assert is_owned_target(foreign) is False
```

Run: `uv run pytest tests/core/test_skill_install.py -k is_owned_target -v`
Expected: 3 FAIL — `is_owned_target` not defined.

- [ ] **Step 2.4: Implement `is_owned_target()`**

```python
# src/mship/core/skill_install.py — append
def is_owned_target(target: Path) -> bool:
    """True if `target` lives under the current package source or a historical source.

    Path-based; `target` need not exist on disk. Lets us transparently migrate
    symlinks whose intended target dir has since moved.
    """
    target = Path(target)
    candidates = (pkg_skills_source(), *HISTORICAL_SOURCES)
    for src in candidates:
        try:
            target.relative_to(src)
            return True
        except ValueError:
            continue
    return False
```

Run: `uv run pytest tests/core/test_skill_install.py -k is_owned_target -v`
Expected: 3 PASS

- [ ] **Step 2.5: Write failing tests for `refresh_symlink()` collision matrix**

```python
# tests/core/test_skill_install.py — append
from mship.core.skill_install import RefreshOutcome, refresh_symlink


def _setup_owned_src(tmp_path: Path, monkeypatch) -> Path:
    """Create a fake package source dir and patch pkg_skills_source to return it."""
    fake_src = tmp_path / "src_pkg" / "skills"
    monkeypatch.setattr("mship.core.skill_install.pkg_skills_source", lambda: fake_src)
    skill = fake_src / "skill-x"
    skill.mkdir(parents=True)
    return skill


def test_refresh_creates_when_target_missing(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    dst = tmp_path / "agent_dir" / "skill-x"
    outcome = refresh_symlink(src, dst, force=False)
    assert outcome == RefreshOutcome.created
    assert dst.is_symlink()
    assert dst.resolve() == src.resolve()


def test_refresh_replaces_owned_symlink(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    old_src = src.parent / "old-loc"; old_src.mkdir()
    dst = tmp_path / "agent_dir" / "skill-x"; dst.parent.mkdir(parents=True)
    dst.symlink_to(old_src)
    outcome = refresh_symlink(src, dst, force=False)
    assert outcome == RefreshOutcome.replaced
    assert dst.resolve() == src.resolve()


def test_refresh_replaces_dangling_owned_symlink(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    dst = tmp_path / "agent_dir" / "skill-x"; dst.parent.mkdir(parents=True)
    dst.symlink_to(src.parent / "ghost")  # dangling, intended target is owned
    outcome = refresh_symlink(src, dst, force=False)
    assert outcome == RefreshOutcome.replaced


def test_refresh_skips_foreign_symlink_without_force(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    foreign = tmp_path / "user_authored" / "thing"; foreign.mkdir(parents=True)
    dst = tmp_path / "agent_dir" / "skill-x"; dst.parent.mkdir(parents=True)
    dst.symlink_to(foreign)
    outcome = refresh_symlink(src, dst, force=False)
    assert outcome == RefreshOutcome.skipped
    assert dst.resolve() == foreign.resolve()


def test_refresh_skips_real_dir_without_force(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    dst = tmp_path / "agent_dir" / "skill-x"
    dst.mkdir(parents=True)
    (dst / "user_file").write_text("don't clobber me")
    outcome = refresh_symlink(src, dst, force=False)
    assert outcome == RefreshOutcome.skipped
    assert (dst / "user_file").read_text() == "don't clobber me"


def test_refresh_force_replaces_foreign_symlink(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    foreign = tmp_path / "user_authored" / "thing"; foreign.mkdir(parents=True)
    dst = tmp_path / "agent_dir" / "skill-x"; dst.parent.mkdir(parents=True)
    dst.symlink_to(foreign)
    outcome = refresh_symlink(src, dst, force=True)
    assert outcome == RefreshOutcome.replaced
    assert dst.resolve() == src.resolve()


def test_refresh_force_replaces_real_dir(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    dst = tmp_path / "agent_dir" / "skill-x"
    dst.mkdir(parents=True)
    (dst / "user_file").write_text("clobber-me")
    outcome = refresh_symlink(src, dst, force=True)
    assert outcome == RefreshOutcome.replaced
    assert dst.is_symlink()
```

Run: `uv run pytest tests/core/test_skill_install.py -k refresh -v`
Expected: 7 FAIL — `RefreshOutcome` and `refresh_symlink` not defined.

- [ ] **Step 2.6: Implement `refresh_symlink()`**

```python
# src/mship/core/skill_install.py — append
import os
import shutil
from enum import Enum


class RefreshOutcome(str, Enum):
    created = "created"
    replaced = "replaced"
    skipped = "skipped"


def refresh_symlink(src: Path, dst: Path, *, force: bool) -> RefreshOutcome:
    """Idempotently point `dst` at `src`. See spec collision table.

    `dst` may be: nonexistent, an owned-or-historical symlink (resolved or
    dangling), a foreign symlink, or a real file/dir. `force` overrides the
    safe-skip on foreign content.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not dst.exists() and not dst.is_symlink():
        dst.symlink_to(src)
        return RefreshOutcome.created

    if dst.is_symlink():
        intended = Path(os.readlink(dst))
        if not intended.is_absolute():
            intended = (dst.parent / intended).resolve(strict=False)
        if is_owned_target(intended) or force:
            dst.unlink()
            dst.symlink_to(src)
            return RefreshOutcome.replaced
        return RefreshOutcome.skipped

    # Real file or dir at dst.
    if force:
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
        dst.symlink_to(src)
        return RefreshOutcome.replaced
    return RefreshOutcome.skipped
```

Run: `uv run pytest tests/core/test_skill_install.py -k refresh -v`
Expected: 7 PASS

- [ ] **Step 2.7: Commit**

```bash
git add src/mship/core/skill_install.py tests/core/test_skill_install.py
git commit -m "feat(skill_install): pure helpers for source resolution + symlink refresh"
```

---

## Task 3: Per-agent install functions + agent detection (TDD)

**Files:**
- Modify: `src/mship/core/skill_install.py`
- Test: `tests/core/test_skill_install.py`

- [ ] **Step 3.1: Write failing test for `install_for_claude()`**

```python
# tests/core/test_skill_install.py — append
from mship.core.skill_install import AgentInstallResult, install_for_claude


def _seed_fake_pkg_with_skills(tmp_path: Path, monkeypatch, names: list[str]) -> Path:
    fake_pkg = tmp_path / "src_pkg" / "skills"
    for name in names:
        (fake_pkg / name).mkdir(parents=True)
        (fake_pkg / name / "SKILL.md").write_text(f"# {name}\n")
    monkeypatch.setattr("mship.core.skill_install.pkg_skills_source", lambda: fake_pkg)
    return fake_pkg


def test_install_for_claude_symlinks_each_skill(tmp_path: Path, monkeypatch):
    fake_pkg = _seed_fake_pkg_with_skills(tmp_path, monkeypatch, ["alpha", "beta"])
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = install_for_claude(force=False)
    assert isinstance(result, AgentInstallResult)
    assert result.agent == "claude"
    assert result.dest == home / ".claude" / "skills"
    assert result.count == 2
    assert result.replaced == []
    assert result.skipped == []
    for name in ["alpha", "beta"]:
        link = home / ".claude" / "skills" / name
        assert link.is_symlink()
        assert link.resolve() == (fake_pkg / name).resolve()
```

Run: `uv run pytest tests/core/test_skill_install.py::test_install_for_claude_symlinks_each_skill -v`
Expected: FAIL — `AgentInstallResult` and `install_for_claude` not defined.

- [ ] **Step 3.2: Implement `install_for_claude()` + `AgentInstallResult` + `_iter_skill_dirs()`**

```python
# src/mship/core/skill_install.py — append
from dataclasses import dataclass, field


@dataclass
class AgentInstallResult:
    agent: str
    dest: Path
    count: int
    skipped: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)


def _iter_skill_dirs(src: Path) -> list[Path]:
    """All immediate subdirs of `src` containing a SKILL.md."""
    if not src.is_dir():
        return []
    return sorted(p for p in src.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def install_for_claude(*, force: bool = False) -> AgentInstallResult:
    """Symlink each skill into ~/.claude/skills/<name>/."""
    src = pkg_skills_source()
    dest = Path.home() / ".claude" / "skills"
    skipped: list[str] = []
    replaced: list[str] = []
    skill_dirs = _iter_skill_dirs(src)
    for skill_dir in skill_dirs:
        target = dest / skill_dir.name
        outcome = refresh_symlink(skill_dir, target, force=force)
        if outcome == RefreshOutcome.replaced:
            replaced.append(skill_dir.name)
        elif outcome == RefreshOutcome.skipped:
            skipped.append(skill_dir.name)
    count = len(skill_dirs) - len(skipped)
    return AgentInstallResult(
        agent="claude", dest=dest, count=count,
        skipped=skipped, replaced=replaced,
    )
```

Run: `uv run pytest tests/core/test_skill_install.py::test_install_for_claude_symlinks_each_skill -v`
Expected: PASS

- [ ] **Step 3.3: Write failing test for `install_for_codex()`**

```python
# tests/core/test_skill_install.py — append
from mship.core.skill_install import install_for_codex


def test_install_for_codex_creates_one_dir_level_symlink(tmp_path: Path, monkeypatch):
    fake_pkg = _seed_fake_pkg_with_skills(tmp_path, monkeypatch, ["alpha"])
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = install_for_codex(force=False)
    assert result.agent == "codex"
    link = home / ".agents" / "skills" / "mothership"
    assert link.is_symlink()
    assert link.resolve() == fake_pkg.resolve()
    assert result.count == 1   # one dir-level symlink, not per-skill
```

Run: `uv run pytest tests/core/test_skill_install.py::test_install_for_codex_creates_one_dir_level_symlink -v`
Expected: FAIL

- [ ] **Step 3.4: Implement `install_for_codex()`**

```python
# src/mship/core/skill_install.py — append
def install_for_codex(*, force: bool = False) -> AgentInstallResult:
    """One dir-level symlink at ~/.agents/skills/mothership → <pkg>/skills/."""
    src = pkg_skills_source()
    dest = Path.home() / ".agents" / "skills"
    target = dest / "mothership"
    outcome = refresh_symlink(src, target, force=force)
    skipped = ["mothership"] if outcome == RefreshOutcome.skipped else []
    replaced = ["mothership"] if outcome == RefreshOutcome.replaced else []
    return AgentInstallResult(
        agent="codex", dest=dest, count=0 if skipped else 1,
        skipped=skipped, replaced=replaced,
    )
```

Run: `uv run pytest tests/core/test_skill_install.py::test_install_for_codex_creates_one_dir_level_symlink -v`
Expected: PASS

- [ ] **Step 3.5: Add `_detect_agents()` to the same module**

```python
# src/mship/core/skill_install.py — append
import shutil


def _detect_agents() -> dict[str, bool]:
    """Best-effort: agent CLI on PATH or its config dir in $HOME."""
    home = Path.home()
    return {
        "claude": shutil.which("claude") is not None or (home / ".claude").exists(),
        "codex":  shutil.which("codex")  is not None or (home / ".codex").exists(),
        "gemini": shutil.which("gemini") is not None or (home / ".gemini").exists(),
    }
```

(This used to live in `cli/skill.py`. Moving it to core lets us monkey-patch it at `mship.core.skill_install._detect_agents` and have the patch reach every caller — both CLI and doctor.)

- [ ] **Step 3.6: Commit**

```bash
git add src/mship/core/skill_install.py tests/core/test_skill_install.py
git commit -m "feat(skill_install): per-agent install funcs for claude+codex; relocate _detect_agents"
```

---

## Task 4: Rewrite `cli/skill.py` for the new install model

**Files:**
- Modify: `src/mship/cli/skill.py`
- Test: `tests/cli/test_skill.py`

**Critical:** the CLI imports the core module *as a module* (not by-name), so callers see monkey-patches applied at `mship.core.skill_install.X`.

- [ ] **Step 4.1: Replace `tests/cli/test_skill.py` with the new contract**

```python
"""Tests for `mship skill` CLI."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app


runner = CliRunner()


def test_skill_list_returns_package_skill_names():
    result = runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "skills" in data
    assert "working-with-mothership" in data["skills"]


def test_skill_install_for_claude_creates_user_scope_symlinks(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "mship.core.skill_install._detect_agents",
        lambda: {"claude": True, "codex": False, "gemini": False},
    )
    result = runner.invoke(app, ["skill", "install"])
    assert result.exit_code == 0, result.output
    target = home / ".claude" / "skills" / "working-with-mothership" / "SKILL.md"
    assert target.exists(), f"missing: {target}"


def test_skill_install_only_flag_limits_agents(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "mship.core.skill_install._detect_agents",
        lambda: {"claude": True, "codex": True, "gemini": False},
    )
    result = runner.invoke(app, ["skill", "install", "--only", "codex"])
    assert result.exit_code == 0, result.output
    assert (home / ".agents" / "skills" / "mothership").is_symlink()
    assert not (home / ".claude" / "skills").exists()


def test_skill_install_warns_about_legacy_codex_mothership_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    legacy = home / ".codex" / "mothership"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "mship.core.skill_install._detect_agents",
        lambda: {"claude": False, "codex": True, "gemini": False},
    )
    result = runner.invoke(app, ["skill", "install"])
    assert result.exit_code == 0, result.output
    assert "no longer used" in result.output
    assert ".codex/mothership" in result.output
```

Run: `uv run pytest tests/cli/test_skill.py -v`
Expected: tests FAIL (CLI doesn't have the new shape yet).

- [ ] **Step 4.2: Rewrite `src/mship/cli/skill.py`**

```python
"""`mship skill` — list and install mship-bundled skills into agent dirs.

Skills ship as package data under src/mship/skills/. The CLI is a thin
wrapper around `mship.core.skill_install`, imported as a module so test
monkey-patches at `mship.core.skill_install.X` take effect here.

See docs/superpowers/specs/2026-04-17-claude-skill-install-discoverability-design.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output
from mship.core import skill_install as _si
from mship.core.skill_install import AgentInstallResult


SUPPORTED_AGENTS = ("claude", "codex", "gemini")


_INSTALLERS = {
    "claude": _si.install_for_claude,
    "codex":  _si.install_for_codex,
    # gemini is not bundled — it has its own `gemini extensions install` flow,
    # which lives outside mship's symlink model. Print guidance instead.
}


def _legacy_codex_mothership_warning(output: Output) -> None:
    legacy = Path.home() / ".codex" / "mothership"
    if legacy.exists():
        output.warning(
            f"old skills source `{legacy}` no longer used; "
            "safe to `rm -rf` it"
        )


def register(app: typer.Typer, get_container):
    @app.command(name="skill")
    def skill_cmd(
        action: str = typer.Argument(help="Action: install | list"),
        only: Optional[str] = typer.Option(None, "--only", help="Comma-separated agents (claude,codex,gemini)"),
        force: bool = typer.Option(False, "--force", help="Override safe-skip on foreign content"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip per-agent confirmation prompts"),
    ):
        """Install or list mship-bundled skills."""
        output = Output()
        if action == "list":
            _list(output)
            return
        if action == "install":
            _install(output, only=only, force=force, yes=yes)
            return
        output.error(f"Unknown action: {action}. Use 'install' or 'list'.")
        raise typer.Exit(code=1)


def _list(output: Output) -> None:
    skills = [d.name for d in _si._iter_skill_dirs(_si.pkg_skills_source())]
    output.json({"skills": skills})


def _install(output: Output, *, only: Optional[str], force: bool, yes: bool) -> None:
    detected = _si._detect_agents()
    if only:
        wanted = {a.strip() for a in only.split(",") if a.strip()}
        unknown = wanted - set(SUPPORTED_AGENTS)
        if unknown:
            output.error(f"Unknown agent(s): {', '.join(sorted(unknown))}")
            raise typer.Exit(code=1)
        targets = sorted(wanted)
    else:
        targets = sorted(a for a, present in detected.items() if present)

    _legacy_codex_mothership_warning(output)

    results: list[AgentInstallResult] = []
    for agent in targets:
        installer = _INSTALLERS.get(agent)
        if installer is None:
            output.print(f"  {agent}: bundled install not supported (use the agent's native flow)")
            continue
        results.append(installer(force=force))

    if output.is_tty:
        for r in results:
            extras = []
            if r.replaced:
                extras.append(f"{len(r.replaced)} refreshed")
            if r.skipped:
                extras.append(f"{len(r.skipped)} skipped (use --force)")
            tail = f" ({', '.join(extras)})" if extras else ""
            output.print(f"  {r.agent}: {r.count} skills installed → {r.dest}{tail}")
    else:
        output.json({"installed": [
            {"agent": r.agent, "dest": str(r.dest), "count": r.count,
             "skipped": r.skipped, "replaced": r.replaced}
            for r in results
        ]})
```

Run: `uv run pytest tests/cli/test_skill.py -v`
Expected: 4 PASS

- [ ] **Step 4.3: Smoke-test the rewritten CLI by hand**

```bash
uv run mship skill list
# Expected JSON: {"skills": ["brainstorming", ...]}

uv run mship skill install --only claude
# Expected line: "claude: 16 skills installed → ~/.claude/skills/"
ls ~/.claude/skills/working-with-mothership/SKILL.md
# Expected: file exists (resolves through the symlink)
```

- [ ] **Step 4.4: Commit**

```bash
git add src/mship/cli/skill.py tests/cli/test_skill.py
git commit -m "feat(skill): rewrite install for direct symlink to package source"
```

---

## Task 5: Doctor skill-availability check

**Files:**
- Modify: `src/mship/core/doctor.py`
- Test: `tests/core/test_doctor.py`

**Critical:** doctor.py also imports skill_install as a module (`from mship.core import skill_install as _si`) for the same patch-visibility reason as the CLI.

- [ ] **Step 5.1: Write failing tests for the doctor skill check**

```python
# tests/core/test_doctor.py — append
from pathlib import Path

from mship.core.doctor import check_skill_availability


def _seed_pkg_and_home(tmp_path: Path, monkeypatch, skill_names: list[str]) -> Path:
    fake_pkg = tmp_path / "src_pkg" / "skills"
    for n in skill_names:
        (fake_pkg / n).mkdir(parents=True)
        (fake_pkg / n / "SKILL.md").write_text(f"# {n}\n")
    monkeypatch.setattr("mship.core.skill_install.pkg_skills_source", lambda: fake_pkg)
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return fake_pkg


def test_skill_check_reports_full_install(tmp_path, monkeypatch):
    fake_pkg = _seed_pkg_and_home(tmp_path, monkeypatch, ["a", "b", "c"])
    monkeypatch.setattr(
        "mship.core.skill_install._detect_agents",
        lambda: {"claude": True, "codex": False, "gemini": False},
    )
    from mship.core.skill_install import install_for_claude
    install_for_claude(force=False)

    results = check_skill_availability()
    by_name = {r.name: r for r in results}
    assert by_name["skills/claude"].status == "pass"
    assert "3/3" in by_name["skills/claude"].message


def test_skill_check_reports_missing_install(tmp_path, monkeypatch):
    _seed_pkg_and_home(tmp_path, monkeypatch, ["a"])
    monkeypatch.setattr(
        "mship.core.skill_install._detect_agents",
        lambda: {"claude": True, "codex": False, "gemini": False},
    )
    results = check_skill_availability()
    by_name = {r.name: r for r in results}
    assert by_name["skills/claude"].status == "warn"
    assert "0/1" in by_name["skills/claude"].message
    assert "mship skill install" in by_name["skills/claude"].message
```

Run: `uv run pytest tests/core/test_doctor.py -k skill_check -v`
Expected: 2 FAIL — `check_skill_availability` not defined.

- [ ] **Step 5.2: Implement `check_skill_availability()` in `core/doctor.py`**

Add the imports at the top of `src/mship/core/doctor.py` (alongside existing imports):

```python
import os

from mship.core import skill_install as _si
```

Then add the function (above `class DoctorChecker`):

```python
def _claude_target(skill_name: str) -> Path:
    return Path.home() / ".claude" / "skills" / skill_name


def _codex_target() -> Path:
    return Path.home() / ".agents" / "skills" / "mothership"


def _intended_target(symlink: Path) -> Path:
    """Read symlink without resolving — works for dangling links."""
    raw = Path(os.readlink(symlink))
    if not raw.is_absolute():
        raw = (symlink.parent / raw).resolve(strict=False)
    return raw


def check_skill_availability() -> list[CheckResult]:
    """One CheckResult per detected agent reporting installed/dangling/foreign."""
    results: list[CheckResult] = []
    pkg_src = _si.pkg_skills_source()
    skill_dirs = _si._iter_skill_dirs(pkg_src)
    total = len(skill_dirs)
    detected = _si._detect_agents()

    if detected.get("claude"):
        installed = dangling = foreign = 0
        for d in skill_dirs:
            target = _claude_target(d.name)
            if not target.exists() and not target.is_symlink():
                continue
            if target.is_symlink():
                intended = _intended_target(target)
                if target.exists() and intended.resolve() == d.resolve():
                    installed += 1
                elif _si.is_owned_target(intended):
                    dangling += 1
                else:
                    foreign += 1
            else:
                foreign += 1
        results.append(_format_skill_check("claude", installed, dangling, foreign, total))

    if detected.get("codex"):
        target = _codex_target()
        installed = dangling = foreign = 0
        if target.is_symlink():
            intended = _intended_target(target)
            if target.exists() and intended.resolve() == pkg_src.resolve():
                installed = total
            elif _si.is_owned_target(intended):
                dangling = total
            else:
                foreign = total
        elif target.exists():
            foreign = total
        results.append(_format_skill_check("codex", installed, dangling, foreign, total))

    return results


def _format_skill_check(agent: str, installed: int, dangling: int, foreign: int, total: int) -> CheckResult:
    if installed == total and dangling == 0 and foreign == 0:
        return CheckResult(
            name=f"skills/{agent}", status="pass",
            message=f"{installed}/{total} skills installed and current",
        )
    parts = [f"{installed}/{total} installed"]
    if dangling:
        parts.append(f"{dangling} dangling")
    if foreign:
        parts.append(f"{foreign} foreign (skipped)")
    msg = ", ".join(parts) + " — run `mship skill install`"
    if foreign:
        msg += " (use --force to overwrite foreign entries)"
    return CheckResult(name=f"skills/{agent}", status="warn", message=msg)
```

Run: `uv run pytest tests/core/test_doctor.py -k skill_check -v`
Expected: 2 PASS

- [ ] **Step 5.3: Wire `check_skill_availability()` into `DoctorChecker.run()`**

In `src/mship/core/doctor.py`, append before `return report` at the end of `DoctorChecker.run()`:

```python
        # Append skill-availability checks (workspace-independent)
        report.checks.extend(check_skill_availability())
```

- [ ] **Step 5.4: Run the full doctor suite to confirm no regressions**

Run: `uv run pytest tests/core/test_doctor.py tests/cli/test_doctor.py -v`
Expected: all PASS

- [ ] **Step 5.5: Commit**

```bash
git add src/mship/core/doctor.py tests/core/test_doctor.py
git commit -m "feat(doctor): per-agent skill-availability check"
```

---

## Task 6: Manual smoke test (gate before merge)

**Goal:** validate the spec's load-bearing assumption — symlinks survive `uv tool upgrade`.

- [ ] **Step 6.1: Fresh-install scenario**

```bash
uv tool install --reinstall --from . mothership   # install from current branch
mship skill install --only claude
ls -la ~/.claude/skills/working-with-mothership   # symlink → uv tool path
# In a NEW terminal:
claude   # confirm `working-with-mothership` appears in available-skills list
```

- [ ] **Step 6.2: Upgrade scenario**

```bash
# Make a trivial edit to one skill (add a date marker line):
echo "<!-- smoke-test: $(date) -->" >> src/mship/skills/working-with-mothership/SKILL.md
git commit -am "smoke: edit working-with-mothership"
uv tool install --reinstall --from . mothership   # simulates `uv tool upgrade`
# Symlink should still resolve, content should reflect the edit:
grep "smoke-test" ~/.claude/skills/working-with-mothership/SKILL.md
# In a NEW claude session: ask the agent to read working-with-mothership and
# confirm it sees the new date marker line.
```

- [ ] **Step 6.3: If smoke fails (symlink dangles after reinstall)**

The package install path changed across reinstalls. Document the failure mode in the PR description, fall back to **copy** mode in `refresh_symlink()` (replace `dst.symlink_to(src)` with `shutil.copytree(src, dst)`), and add a doctor-suggested re-install on every `mship` upgrade. Note: do NOT proceed to merge without resolving this.

- [ ] **Step 6.4: Revert the smoke-test commit**

```bash
git reset --hard HEAD~1
```

---

## Task 7: Final verification + PR

- [ ] **Step 7.1: Full test suite green**

```bash
uv run pytest -x -q
# Expected: all pass (768 prior + ~14 new ≈ 782)
```

- [ ] **Step 7.2: Spec coverage check**

For each requirement in the spec, confirm a task implements it:
- ✅ User-scope `~/.claude/skills/<name>/` install — Task 3.1/3.2
- ✅ Codex `~/.agents/skills/mothership` symlink — Task 3.3/3.4
- ✅ Package-bundled source — Task 1
- ✅ Collision matrix (6 rows) — Task 2.5/2.6
- ✅ `--force` override — Task 2.6, Task 4.2
- ✅ `--only` flag — Task 4.2
- ✅ Removed: `--all`, `<name>` positional, `--dest`, slash-command output — Task 4.2
- ✅ Legacy `~/.codex/mothership/` deprecation message — Task 4.2
- ✅ Doctor skill-availability check — Task 5
- ✅ Smoke test confirming symlink survival — Task 6

- [ ] **Step 7.3: Open the PR via `mship finish`**

```bash
mship finish
```

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-04-17-claude-skill-install-discoverability.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks
2. **Inline Execution** — execute tasks in this session with checkpoints

Which approach?
