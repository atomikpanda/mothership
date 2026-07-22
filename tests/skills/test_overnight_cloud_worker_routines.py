"""Guard: the overnight-cloud-worker-routines skill documents the real command
flow (the actual flag names) and the guarantees, so it can't drift from the CLI."""
from __future__ import annotations

from mship.core.skill_install import pkg_skills_source


def _skill_text() -> str:
    return (
        pkg_skills_source() / "overnight-cloud-worker-routines" / "SKILL.md"
    ).read_text()


def test_skill_has_frontmatter():
    text = _skill_text()
    assert text.startswith("---")
    assert "name: overnight-cloud-worker-routines" in text
    assert "description:" in text


def test_skill_documents_the_command_flow():
    text = _skill_text()
    assert "mship relay issue-run-token" in text
    assert "mship bootstrap --relay-url" in text
    assert "mship gh preflight --relay-url" in text


def test_skill_states_the_guarantees():
    low = _skill_text().lower()
    assert "run token" in low
    # nothing auto-merges / review-gated
    assert "auto-merge" in low or "review-gated" in low
    # the run token grant must cover the workspace repo + the member repos
    assert "workspace" in low
    assert "--push-branch" in _skill_text()
