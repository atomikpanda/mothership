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

import os
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import mship


@dataclass
class AgentInstallResult:
    agent: str
    dest: Path
    count: int
    skipped: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)


# Historical source paths recognized as "owned by mship" so old symlinks from
# previous install layouts get transparently refreshed instead of safe-skipped.
# Append (never remove) entries when the canonical source path changes.
HISTORICAL_SOURCES: tuple[Path, ...] = (
    Path.home() / ".codex" / "mothership" / "skills",
)


def pkg_skills_source() -> Path:
    """Return the package-bundled skills directory."""
    return Path(mship.__file__).parent / "skills"


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


def _detect_agents() -> dict[str, bool]:
    """Best-effort: agent CLI on PATH or its config dir in $HOME."""
    home = Path.home()
    return {
        "claude": shutil.which("claude") is not None or (home / ".claude").exists(),
        "codex":  shutil.which("codex")  is not None or (home / ".codex").exists(),
        "gemini": shutil.which("gemini") is not None or (home / ".gemini").exists(),
    }
