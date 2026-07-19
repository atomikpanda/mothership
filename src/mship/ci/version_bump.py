"""Compute and apply the next package version for the CI version-bump workflow (issue 376).

The version lives in two files that must stay in sync (guarded by tests/test_version.py):
pyproject.toml [project].version and src/mship/__init__.py __version__. This module reads the
current version from pyproject.toml, computes the next semver from a PR-label-derived bump
level, and rewrites both files in place.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Iterable

_LEVELS = ("major", "minor", "patch")  # highest-precedence first
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# In-place single-line substitutions that preserve all other formatting.
_PYPROJECT_VERSION_RE = re.compile(
    r'(?P<pre>^version\s*=\s*")(?P<ver>\d+\.\d+\.\d+)(?P<post>")', re.MULTILINE
)
_INIT_VERSION_RE = re.compile(
    r'(?P<pre>^__version__\s*=\s*")(?P<ver>\d+\.\d+\.\d+)(?P<post>")', re.MULTILINE
)


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


_LABEL_PREFIX = "semver:"


def select_level(labels: Iterable[str]) -> str:
    names = {label.strip().lower() for label in labels if label and label.strip()}
    for level in _LEVELS:  # major, minor, patch -> precedence major > minor > patch
        if f"{_LABEL_PREFIX}{level}" in names:
            return level
    return "patch"


def read_current_version(pyproject_path: Path) -> str:
    data = tomllib.loads(Path(pyproject_path).read_text(encoding="utf-8"))
    try:
        return data["project"]["version"]
    except (KeyError, TypeError) as exc:
        raise VersionError(f"no [project].version in {pyproject_path}") from exc


def _sub_once(text: str, pattern: re.Pattern[str], new_version: str, where: Path) -> str:
    new_text, n = pattern.subn(
        lambda m: f"{m.group('pre')}{new_version}{m.group('post')}", text, count=1
    )
    if n != 1:
        raise VersionError(f"could not find a version line to update in {where}")
    return new_text


def rewrite_version_files(repo_root: Path, new_version: str) -> None:
    repo_root = Path(repo_root)
    pyproject = repo_root / "pyproject.toml"
    init = repo_root / "src" / "mship" / "__init__.py"
    # Compute BOTH substitutions before writing EITHER, so a failure leaves both files intact.
    new_py = _sub_once(pyproject.read_text(encoding="utf-8"), _PYPROJECT_VERSION_RE, new_version, pyproject)
    new_init = _sub_once(init.read_text(encoding="utf-8"), _INIT_VERSION_RE, new_version, init)
    pyproject.write_text(new_py, encoding="utf-8")
    init.write_text(new_init, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mship.ci.version_bump")
    parser.add_argument("--labels", default="", help="comma- or newline-separated PR label names")
    parser.add_argument("--repo-root", default=".", help="repo root containing pyproject.toml")
    args = parser.parse_args(argv)

    labels = re.split(r"[,\n]", args.labels)
    level = select_level(labels)
    repo_root = Path(args.repo_root).resolve()
    current = read_current_version(repo_root / "pyproject.toml")
    new_version = bump_version(current, level)
    rewrite_version_files(repo_root, new_version)
    print(new_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
