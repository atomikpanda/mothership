"""Compute and apply the next package version for the CI version-bump workflow (issue 376).

The version lives in two files that must stay in sync (guarded by tests/test_version.py):
pyproject.toml [project].version and src/mship/__init__.py __version__. This module reads the
current version from pyproject.toml, computes the next semver from a PR-label-derived bump
level, and rewrites both files in place.
"""
from __future__ import annotations

import re
from typing import Iterable

_LEVELS = ("major", "minor", "patch")  # highest-precedence first
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class VersionError(ValueError):
    """Raised when a version can't be parsed or a level is unknown."""


def bump_version(current: str, level: str) -> str:
    m = _VERSION_RE.match(current.strip())
    if not m:
        raise VersionError(f"not a MAJOR.MINOR.PATCH version: {current!r}")
    if level not in _LEVELS:
        raise VersionError(f"unknown bump level: {level!r}")
    major, minor, patch = (int(g) for g in m.groups())
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"
