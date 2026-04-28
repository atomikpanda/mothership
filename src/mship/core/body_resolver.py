"""Resolve per-PR bodies for `mship finish --body-map`. See #114.

Mirrors the shape of `base_resolver.py`. Body values are file paths read at
finish time, not inline strings — bodies tend to be multi-line markdown that
the user already has in a file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


class InvalidBodyMapError(ValueError):
    pass


class UnknownRepoInBodyMapError(ValueError):
    pass


class EmptyBodyInMapError(ValueError):
    pass


def parse_body_map(raw: str) -> dict[str, str]:
    """Parse 'repoA=path,repoB=path' into a dict.

    Empty input returns {}. Whitespace around keys, values, and separators is
    stripped. Raises InvalidBodyMapError on malformed input.
    """
    if not raw or not raw.strip():
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            raise InvalidBodyMapError(
                f"Invalid --body-map entry {pair!r}: expected repo=path"
            )
        key, _, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise InvalidBodyMapError(
                f"Invalid --body-map entry {pair!r}: key and value must be non-empty"
            )
        out[key] = value
    return out


def load_body_map(
    body_map: dict[str, str],
    known_repos: Iterable[str],
) -> dict[str, str]:
    """Validate repos and read each file. Returns {repo: body_text}.

    Raises:
        UnknownRepoInBodyMapError: a key isn't in `known_repos`.
        InvalidBodyMapError: a path can't be read.
        EmptyBodyInMapError: a file is empty (whitespace-only counts as empty).

    Empty input returns {} so callers can unconditionally call this and fall
    back to the catch-all body source when the map is empty.
    """
    if not body_map:
        return {}
    known = set(known_repos)
    unknown = [r for r in body_map if r not in known]
    if unknown:
        raise UnknownRepoInBodyMapError(
            f"Unknown repo(s) in --body-map: {', '.join(sorted(unknown))}. "
            f"Known: {sorted(known)}"
        )
    out: dict[str, str] = {}
    for repo, path in body_map.items():
        try:
            content = Path(path).read_text()
        except OSError as e:
            raise InvalidBodyMapError(
                f"Could not read --body-map file for {repo!r}: {path}: {e}"
            )
        if not content.strip():
            raise EmptyBodyInMapError(
                f"PR body for {repo!r} is empty: {path}. "
                f"Write a Summary + Test plan, or omit the entry to fall back."
            )
        out[repo] = content
    return out
