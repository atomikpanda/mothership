"""Guard: working-with-mothership documents mship capture."""
from __future__ import annotations

from mship.core.skill_install import pkg_skills_source


def test_working_with_mothership_documents_capture():
    text = (pkg_skills_source() / "working-with-mothership" / "SKILL.md").read_text()
    assert "mship capture" in text
    assert "capture" in text.lower() and "ui" in text.lower()
