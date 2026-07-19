# tests/ci/test_version_bump.py
from pathlib import Path

import pytest

from mship.ci.version_bump import (
    VersionError,
    bump_version,
    read_current_version,
    rewrite_version_files,
    select_level,
)


@pytest.mark.parametrize(
    "current,level,expected",
    [
        ("0.5.0", "patch", "0.5.1"),
        ("0.5.0", "minor", "0.6.0"),   # patch digit zeroed
        ("0.5.0", "major", "1.0.0"),   # minor and patch zeroed
        ("1.2.3", "patch", "1.2.4"),
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "major", "2.0.0"),
    ],
)
def test_bump_version(current, level, expected):
    assert bump_version(current, level) == expected


def test_bump_version_rejects_bad_version():
    with pytest.raises(VersionError):
        bump_version("1.2", "patch")


def test_bump_version_rejects_bad_level():
    with pytest.raises(VersionError):
        bump_version("1.2.3", "sideways")


@pytest.mark.parametrize(
    "labels,expected",
    [
        (["semver:minor"], "minor"),
        (["semver:patch", "semver:minor"], "minor"),          # highest precedence wins
        ([], "patch"),                                         # default
        (["bug", "needs-review"], "patch"),                   # no semver label -> default
        (["semver:major", "semver:patch"], "major"),
        (["SemVer:Major"], "major"),                          # case-insensitive
    ],
)
def test_select_level(labels, expected):
    assert select_level(labels) == expected


def _mini_repo(tmp_path: Path, version: str = "0.5.0") -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "mship"\nversion = "{version}"\ndescription = "x"\n',
        encoding="utf-8",
    )
    pkg = tmp_path / "src" / "mship"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        f'"""mship."""\n__version__ = "{version}"\n', encoding="utf-8"
    )
    return tmp_path


def test_read_current_version(tmp_path):
    repo = _mini_repo(tmp_path, "1.2.3")
    assert read_current_version(repo / "pyproject.toml") == "1.2.3"


def test_rewrite_updates_both_files_and_leaves_rest_intact(tmp_path):
    repo = _mini_repo(tmp_path, "0.5.0")
    rewrite_version_files(repo, "0.6.0")

    py = (repo / "pyproject.toml").read_text(encoding="utf-8")
    init = (repo / "src" / "mship" / "__init__.py").read_text(encoding="utf-8")

    assert 'version = "0.6.0"' in py
    assert '__version__ = "0.6.0"' in init
    # Surrounding lines untouched:
    assert 'name = "mship"' in py and 'description = "x"' in py
    assert py.startswith("[project]")
    assert init.startswith('"""mship."""')
    # The two declarations agree (mirrors the real tests/test_version.py guard):
    assert '0.6.0' in py and '0.6.0' in init


def test_rewrite_leaves_both_files_untouched_when_init_line_missing(tmp_path):
    repo = _mini_repo(tmp_path, "0.5.0")
    # Corrupt the __init__ so its version line can't be found.
    (repo / "src" / "mship" / "__init__.py").write_text('"""mship."""\n', encoding="utf-8")
    before_py = (repo / "pyproject.toml").read_text(encoding="utf-8")

    with pytest.raises(VersionError):
        rewrite_version_files(repo, "0.6.0")

    # pyproject must be untouched because we raise before writing anything.
    assert (repo / "pyproject.toml").read_text(encoding="utf-8") == before_py


def test_read_current_version_raises_when_absent(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(VersionError):
        read_current_version(tmp_path / "pyproject.toml")
