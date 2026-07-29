"""Guard the bundled skills against drifting from the dispatch-ergonomics workflow."""
from __future__ import annotations

from pathlib import Path

from mship.core.skill_install import pkg_skills_source

_REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_working_with_mothership_documents_stub_and_emit():
    text = _read("working-with-mothership")
    assert "--emit" in text          # subagent derives its own prompt
    assert "stub" in text.lower()    # controller gets a pointer, not the prompt


def test_working_with_mothership_documents_model_resolution():
    text = _read("working-with-mothership")
    assert "dispatch_models" in text


def test_configuration_docs_cover_dispatch_models():
    assert "dispatch_models" in (_REPO_ROOT / "docs" / "configuration.md").read_text()


def _skill_markdown_files() -> list[Path]:
    return sorted(pkg_skills_source().rglob("*.md"))


def test_no_skill_instructs_require_tests_flag():
    """Skills must describe the gate as the default, never instruct passing
    `mship finish --require-tests` (now a deprecated no-op)."""
    for path in _skill_markdown_files():
        text = path.read_text()
        assert "finish --require-tests" not in text, (
            f"{path} still instructs the deprecated --require-tests flag"
        )
