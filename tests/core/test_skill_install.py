"""Unit tests for src/mship/core/skill_install.py."""
from __future__ import annotations

from pathlib import Path

import mship
from mship.core.skill_install import (
    HISTORICAL_SOURCES,
    RefreshOutcome,
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
