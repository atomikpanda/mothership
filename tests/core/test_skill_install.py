"""Unit tests for src/mship/core/skill_install.py."""
from __future__ import annotations

from pathlib import Path

import mship
from mship.core.skill_install import (
    HISTORICAL_SOURCES,
    AgentInstallResult,
    RefreshOutcome,
    install_for_claude,
    install_for_codex,
    is_owned_target,
    pkg_skills_source,
    refresh_symlink,
)


def test_pkg_skills_source_resolves_to_package_dir():
    src = pkg_skills_source()
    assert src == Path(mship.__file__).parent / "skills"
    assert src.is_dir()
    assert (src / "working-with-mothership" / "SKILL.md").is_file()


# --- is_owned_target -------------------------------------------------------


def test_is_owned_target_recognizes_current_pkg_source():
    target_inside_pkg = pkg_skills_source() / "working-with-mothership"
    assert is_owned_target(target_inside_pkg) is True


def test_is_owned_target_recognizes_historical_source():
    historical = HISTORICAL_SOURCES[0]
    target = historical / "foo"
    assert is_owned_target(target) is True


def test_is_owned_target_returns_false_for_foreign_path(tmp_path: Path):
    foreign = tmp_path / "elsewhere" / "skill"
    assert is_owned_target(foreign) is False


# --- refresh_symlink collision matrix --------------------------------------


def _setup_owned_src(tmp_path: Path, monkeypatch) -> Path:
    """Create a fake package source dir and patch pkg_skills_source to return it."""
    fake_src = tmp_path / "src_pkg" / "skills"
    monkeypatch.setattr("mship.core.skill_install.pkg_skills_source", lambda: fake_src)
    skill = fake_src / "skill-x"
    skill.mkdir(parents=True)
    return skill


def test_refresh_creates_when_target_missing(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    dst = tmp_path / "agent_dir" / "skill-x"
    outcome = refresh_symlink(src, dst, force=False)
    assert outcome == RefreshOutcome.created
    assert dst.is_symlink()
    assert dst.resolve() == src.resolve()


def test_refresh_replaces_owned_symlink(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    old_src = src.parent / "old-loc"; old_src.mkdir()
    dst = tmp_path / "agent_dir" / "skill-x"; dst.parent.mkdir(parents=True)
    dst.symlink_to(old_src)
    outcome = refresh_symlink(src, dst, force=False)
    assert outcome == RefreshOutcome.replaced
    assert dst.resolve() == src.resolve()


def test_refresh_replaces_dangling_owned_symlink(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    dst = tmp_path / "agent_dir" / "skill-x"; dst.parent.mkdir(parents=True)
    dst.symlink_to(src.parent / "ghost")  # dangling, intended target is owned
    outcome = refresh_symlink(src, dst, force=False)
    assert outcome == RefreshOutcome.replaced


def test_refresh_skips_foreign_symlink_without_force(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    foreign = tmp_path / "user_authored" / "thing"; foreign.mkdir(parents=True)
    dst = tmp_path / "agent_dir" / "skill-x"; dst.parent.mkdir(parents=True)
    dst.symlink_to(foreign)
    outcome = refresh_symlink(src, dst, force=False)
    assert outcome == RefreshOutcome.skipped
    assert dst.resolve() == foreign.resolve()


def test_refresh_skips_real_dir_without_force(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    dst = tmp_path / "agent_dir" / "skill-x"
    dst.mkdir(parents=True)
    (dst / "user_file").write_text("don't clobber me")
    outcome = refresh_symlink(src, dst, force=False)
    assert outcome == RefreshOutcome.skipped
    assert (dst / "user_file").read_text() == "don't clobber me"


def test_refresh_force_replaces_foreign_symlink(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    foreign = tmp_path / "user_authored" / "thing"; foreign.mkdir(parents=True)
    dst = tmp_path / "agent_dir" / "skill-x"; dst.parent.mkdir(parents=True)
    dst.symlink_to(foreign)
    outcome = refresh_symlink(src, dst, force=True)
    assert outcome == RefreshOutcome.replaced
    assert dst.resolve() == src.resolve()


def test_refresh_force_replaces_real_dir(tmp_path: Path, monkeypatch):
    src = _setup_owned_src(tmp_path, monkeypatch)
    dst = tmp_path / "agent_dir" / "skill-x"
    dst.mkdir(parents=True)
    (dst / "user_file").write_text("clobber-me")
    outcome = refresh_symlink(src, dst, force=True)
    assert outcome == RefreshOutcome.replaced
    assert dst.is_symlink()


# --- per-agent installers --------------------------------------------------


def _seed_fake_pkg_with_skills(tmp_path: Path, monkeypatch, names: list[str]) -> Path:
    fake_pkg = tmp_path / "src_pkg" / "skills"
    for name in names:
        (fake_pkg / name).mkdir(parents=True)
        (fake_pkg / name / "SKILL.md").write_text(f"# {name}\n")
    monkeypatch.setattr("mship.core.skill_install.pkg_skills_source", lambda: fake_pkg)
    return fake_pkg


def test_install_for_claude_symlinks_each_skill(tmp_path: Path, monkeypatch):
    fake_pkg = _seed_fake_pkg_with_skills(tmp_path, monkeypatch, ["alpha", "beta"])
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = install_for_claude(force=False)
    assert isinstance(result, AgentInstallResult)
    assert result.agent == "claude"
    assert result.dest == home / ".claude" / "skills"
    assert result.count == 2
    assert result.replaced == []
    assert result.skipped == []
    for name in ["alpha", "beta"]:
        link = home / ".claude" / "skills" / name
        assert link.is_symlink()
        assert link.resolve() == (fake_pkg / name).resolve()


def test_install_for_codex_creates_one_dir_level_symlink(tmp_path: Path, monkeypatch):
    fake_pkg = _seed_fake_pkg_with_skills(tmp_path, monkeypatch, ["alpha"])
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = install_for_codex(force=False)
    assert result.agent == "codex"
    link = home / ".agents" / "skills" / "mothership"
    assert link.is_symlink()
    assert link.resolve() == fake_pkg.resolve()
    assert result.count == 1


# --- Renamed-skills sweep (spec 2026-04-19) ---


def test_sweep_removes_stale_owned_symlink(tmp_path, monkeypatch):
    """An owned (mship-originated) symlink at a renamed location is removed."""
    import mship.core.skill_install as si

    # Fake home → tmp_path so `~/.claude/skills/...` lands in the sandbox.
    monkeypatch.setenv("HOME", str(tmp_path))

    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    pkg_src = si.pkg_skills_source()

    # Simulate a stale symlink: old name pointing at the package source (owned).
    stale = skills_dir / "using-superpowers"
    stale.symlink_to(pkg_src / "using-superpowers")  # may not exist on disk; dangling is OK

    # Test the rename-map directly (no need to install everything).
    si._sweep_renamed_skills(skills_dir, {"using-superpowers": "using-mothership"})

    assert not stale.exists() and not stale.is_symlink(), "stale symlink should be removed"


def test_sweep_preserves_foreign_symlink(tmp_path, monkeypatch):
    """A symlink at the renamed location pointing OUTSIDE mship's tree is left alone."""
    import mship.core.skill_install as si

    monkeypatch.setenv("HOME", str(tmp_path))
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    foreign_target = tmp_path / "elsewhere"
    foreign_target.mkdir()
    foreign = skills_dir / "using-superpowers"
    foreign.symlink_to(foreign_target)

    si._sweep_renamed_skills(skills_dir, {"using-superpowers": "using-mothership"})

    assert foreign.is_symlink(), "foreign symlink should be preserved"
    assert foreign.resolve() == foreign_target.resolve()


def test_sweep_preserves_regular_file(tmp_path, monkeypatch):
    """A regular file at the renamed location (user replaced the symlink) is preserved."""
    import mship.core.skill_install as si

    monkeypatch.setenv("HOME", str(tmp_path))
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    # Regular directory (user replaced symlink with their own content)
    (skills_dir / "using-superpowers").mkdir()
    (skills_dir / "using-superpowers" / "SKILL.md").write_text("my own version")

    si._sweep_renamed_skills(skills_dir, {"using-superpowers": "using-mothership"})

    assert (skills_dir / "using-superpowers").is_dir(), "regular directory should be preserved"


def test_sweep_noop_when_old_name_absent(tmp_path, monkeypatch):
    """No stale entry → no-op, no error."""
    import mship.core.skill_install as si

    monkeypatch.setenv("HOME", str(tmp_path))
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    # Nothing to sweep.
    si._sweep_renamed_skills(skills_dir, {"using-superpowers": "using-mothership"})


def test_install_for_claude_runs_sweep(tmp_path, monkeypatch):
    """install_for_claude invokes the sweep with the canonical _RENAMED_SKILLS map."""
    import mship.core.skill_install as si

    # Seed fake pkg without using-superpowers, so sweep removes it and nothing recreates it.
    fake_pkg = _seed_fake_pkg_with_skills(tmp_path, monkeypatch, ["other-skill"])
    monkeypatch.setenv("HOME", str(tmp_path))
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    # Create stale symlink pointing to historical location (owned by mship).
    stale = skills_dir / "using-superpowers"
    stale.symlink_to(fake_pkg / "using-superpowers")  # dangling; intended target would be owned

    si.install_for_claude(force=False)

    # Sweep should remove the stale symlink because it points to a mship-owned location.
    assert not stale.exists() and not stale.is_symlink()
