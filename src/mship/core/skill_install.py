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
