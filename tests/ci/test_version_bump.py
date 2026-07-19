# tests/ci/test_version_bump.py
import pytest

from mship.ci.version_bump import VersionError, bump_version, select_level


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
