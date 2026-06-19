"""Guard the bundled skills against drifting from the dispatch-ergonomics workflow."""
from __future__ import annotations

from mship.core.skill_install import pkg_skills_source


def _read(skill: str, fname: str = "SKILL.md") -> str:
    return (pkg_skills_source() / skill / fname).read_text()


def test_writing_plans_documents_task_anchors():
    text = _read("writing-plans")
    assert "<!-- mship:task id=" in text
    assert "<!-- /mship:task -->" in text


def test_writing_plans_documents_plan_task_dispatch():
    text = _read("writing-plans")
    assert "--plan-task" in text


def test_sdd_references_plan_task_dispatch():
    text = _read("subagent-driven-development")
    assert "--plan-task" in text


def test_sdd_uses_mship_test_for_evidence():
    text = _read("subagent-driven-development")
    assert "mship test" in text


def test_implementer_prompt_uses_mship_test():
    text = _read("subagent-driven-development", "implementer-prompt.md")
    assert "mship test" in text
