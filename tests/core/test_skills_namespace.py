"""Regression tests for the 2026-04-19 skills audit:
- Namespace cleanup: no `superpowers:<name>` cross-references remain.
- Rename: `using-mothership/` exists with correct frontmatter; old dir gone.
- Coverage: `working-with-mothership` documents multi-task + dispatch/context.
"""
import subprocess
from pathlib import Path


SKILLS_DIR = Path(__file__).resolve().parents[2] / "src" / "mship" / "skills"


def test_no_superpowers_prefix_remains_in_skills():
    """No skill file cross-references `superpowers:<name>` anymore."""
    result = subprocess.run(
        ["grep", "-rn", "superpowers:", str(SKILLS_DIR)],
        capture_output=True, text=True,
    )
    # grep exit codes: 0 = matches, 1 = no matches, 2 = error.
    # We want 1 (no matches).
    assert result.returncode == 1, f"Found leftover superpowers: references:\n{result.stdout}"


def test_using_mothership_directory_exists():
    assert (SKILLS_DIR / "using-mothership").is_dir()
    assert (SKILLS_DIR / "using-mothership" / "SKILL.md").is_file()


def test_using_superpowers_directory_is_gone():
    assert not (SKILLS_DIR / "using-superpowers").exists()


def test_using_mothership_frontmatter_name_is_correct():
    content = (SKILLS_DIR / "using-mothership" / "SKILL.md").read_text()
    assert "name: using-mothership" in content
    assert "name: using-superpowers" not in content


def test_working_with_mothership_covers_multi_task():
    content = (SKILLS_DIR / "working-with-mothership" / "SKILL.md").read_text()
    assert "MSHIP_TASK" in content, "multi-task env var must be documented"
    assert "mship worktrees" in content, "worktrees command should appear in multi-task section"
    assert "multiple tasks" in content.lower(), "Section header or prose should mention multi-task"


def test_working_with_mothership_covers_dispatch_and_context():
    content = (SKILLS_DIR / "working-with-mothership" / "SKILL.md").read_text()
    assert "mship dispatch" in content
    assert "mship context" in content


def test_subagent_driven_development_cross_refs_mship_dispatch():
    content = (SKILLS_DIR / "subagent-driven-development" / "SKILL.md").read_text()
    assert "mship dispatch" in content, "subagent-driven-development should mention mship dispatch"
