"""Guard tests: keep the bundled skills aligned with the CLI after the Tier 2/3 refresh."""
import re
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src" / "mship" / "skills"

EDITED_SKILLS = [
    "working-with-mothership", "brainstorming", "writing-plans", "executing-plans",
    "subagent-driven-development", "using-mothership", "writing-skills",
    "test-driven-development", "verification-before-completion",
    "dispatching-parallel-agents", "using-git-worktrees", "requesting-code-review",
]

# Stale upstream-superpowers PATHS that must not reappear in the refreshed skills.
STALE_PATHS = ["~/.config/superpowers", "docs/superpowers/"]

REAL_SPEC_SUBCOMMANDS = {
    "new", "draft", "apply", "validate", "review", "verdict",
    "questions", "ask", "answer", "approve", "request-changes", "dispatch",
    "from-thread",
}


@pytest.mark.parametrize("skill", EDITED_SKILLS)
def test_no_stale_superpowers_paths(skill):
    # Scan SKILL.md AND sibling files (prompt templates etc.) — drift hides there too.
    for md in sorted((SKILLS_DIR / skill).rglob("*.md")):
        text = md.read_text()
        for stale in STALE_PATHS:
            assert stale not in text, f"{md.relative_to(SKILLS_DIR)}: stale path {stale!r}"


def test_working_with_mothership_names_real_spec_commands():
    text = (SKILLS_DIR / "working-with-mothership" / "SKILL.md").read_text()
    # Only match command-form usages: preceded by a backtick, or at line start
    # (optionally after `$ `) — NOT mid-prose like "the mship spec commands".
    named = set(re.findall(r"(?m)(?:`|^\s*(?:\$ )?)mship spec ([a-z][a-z-]+)", text))
    bogus = named - REAL_SPEC_SUBCOMMANDS
    assert not bogus, f"working-with-mothership names non-existent spec subcommands: {bogus}"


def test_dispatch_examples_include_instruction():
    """Example invocations of `mship dispatch` must carry the required -i/--instruction."""
    for skill in ("working-with-mothership", "subagent-driven-development"):
        text = (SKILLS_DIR / skill / "SKILL.md").read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"^(\$ )?mship dispatch\b", stripped):
                assert "-i" in stripped or "--instruction" in stripped, \
                    f"{skill}: `mship dispatch` example missing -i: {stripped!r}"
