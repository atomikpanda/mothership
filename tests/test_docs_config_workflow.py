"""issue 366 #5/#7: docs must cover the config-change workflow and the
`.worktrees`/`.mothership` bundler caveat, in both README and the skill."""
from pathlib import Path

from mship.core.skill_install import pkg_skills_source

README = Path(__file__).resolve().parent.parent / "README.md"
SKILL = pkg_skills_source() / "working-with-mothership" / "SKILL.md"


def _text(p: Path) -> str:
    return p.read_text().lower()


def test_readme_documents_config_change_workflow():
    t = _text(README)
    assert "mothership.yaml" in t and "mship doctor" in t
    assert "config-only" in t          # the dirty_worktree exemption is named
    assert "require_paths" in t or "not-yet-present" in t


def test_readme_documents_bundler_caveat():
    t = _text(README)
    assert ".worktrees" in t and ".mothership" in t
    assert "code.fromasset" in t and "bundl" in t
    assert "gitignore" in t


def test_skill_documents_config_change_workflow():
    t = _text(SKILL)
    assert "config-only" in t and "mship doctor" in t


def test_skill_documents_bundler_caveat():
    t = _text(SKILL)
    assert ".worktrees" in t and ".mothership" in t
    assert "bundl" in t and "gitignore" in t
