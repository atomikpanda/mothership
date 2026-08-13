"""Guard the bundled skills against drifting from the dispatch-ergonomics workflow."""
from __future__ import annotations

from pathlib import Path

import pytest

from mship.core.skill_install import pkg_skills_source

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(skill: str, fname: str = "SKILL.md") -> str:
    return (pkg_skills_source() / skill / fname).read_text()


_MODEL_ADAPTER_FILES = (
    ("subagent-driven-development", "SKILL.md"),
    ("subagent-driven-development", "implementer-prompt.md"),
    ("subagent-driven-development", "task-reviewer-prompt.md"),
    ("subagent-driven-development", "re-review-prompt.md"),
    ("using-mothership", "references/codex-tools.md"),
    ("using-mothership", "references/pi-tools.md"),
    ("working-with-mothership", "SKILL.md"),
)

_UNSUPPORTED_SELECTOR_ERROR = (
    "mship resolved explicit model '<value>', but this subagent API cannot select "
    "a model; set this mode to inherit or use a selector-capable dispatch tool."
)


def _section(text: str, heading: str) -> str:
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


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


@pytest.mark.parametrize(("skill", "fname"), _MODEL_ADAPTER_FILES)
def test_model_adapter_instructions_define_portable_contract(skill: str, fname: str):
    text = _read(skill, fname)
    assert "`inherit`" in text
    assert "omit" in text
    assert "cannot select a model" in text
    assert "Never translate" in text


@pytest.mark.parametrize(
    "fname",
    ("implementer-prompt.md", "task-reviewer-prompt.md", "re-review-prompt.md"),
)
def test_claude_prompt_templates_apply_explicit_models_or_omit_inherit(fname: str):
    text = _read("subagent-driven-development", fname)
    assert "model: vendor/custom-tier" in text
    assert "omit the entire selector field" in text
    assert _UNSUPPORTED_SELECTOR_ERROR in text


def test_mothership_dispatch_instructions_prescribe_no_provider_tiers():
    texts = {
        "subagent-driven-development model selection": _section(
            _read("subagent-driven-development"), "## Model Selection"
        ),
        "working-with-mothership dispatch": _section(
            _read("working-with-mothership"),
            "## Delegating to subagents: `mship context` and `mship dispatch`",
        ),
        **{
            fname: _read("subagent-driven-development", fname)
            for fname in (
                "implementer-prompt.md",
                "task-reviewer-prompt.md",
                "re-review-prompt.md",
            )
        },
    }
    for source, text in texts.items():
        for provider_tier in ("sonnet", "haiku", "opus"):
            assert provider_tier not in text.lower(), (
                f"{source} prescribes provider tier {provider_tier!r}"
            )


def test_codex_and_pi_references_apply_or_reject_explicit_models():
    codex = _read("using-mothership", "references/codex-tools.md")
    pi = _read("using-mothership", "references/pi-tools.md")

    assert "spawn_agent without a model selector" in codex
    assert "pass it unchanged" in codex
    assert _UNSUPPORTED_SELECTOR_ERROR in codex
    assert "inspect its schema" in pi
    assert "when it exposes a model selector" in pi
    assert _UNSUPPORTED_SELECTOR_ERROR in pi


def test_configuration_docs_define_portable_dispatch_model_defaults():
    text = (_REPO_ROOT / "docs" / "configuration.md").read_text()
    assert "every built-in mode defaults to `inherit`" in text
    assert "harness default" in text
    assert "emitted verbatim" in text
    assert "selector-capable" in text


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
