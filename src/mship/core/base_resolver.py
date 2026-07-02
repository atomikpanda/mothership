"""Resolve effective PR base branch per repo from config + CLI flags."""
from __future__ import annotations

from typing import Iterable


class InvalidBaseMapError(ValueError):
    pass


class UnknownRepoInBaseMapError(ValueError):
    pass


def parse_base_map(raw: str) -> dict[str, str]:
    """Parse 'repoA=branch,repoB=branch' into a dict.

    Empty input returns {}. Whitespace around keys, values, and separators is
    stripped. Raises InvalidBaseMapError on malformed input.
    """
    if not raw or not raw.strip():
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            raise InvalidBaseMapError(
                f"Invalid --base-map entry {pair!r}: expected repo=branch"
            )
        key, _, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise InvalidBaseMapError(
                f"Invalid --base-map entry {pair!r}: key and value must be non-empty"
            )
        out[key] = value
    return out


def resolve_base(
    repo_name: str,
    repo_config,
    cli_base: str | None,
    base_map: dict[str, str],
    known_repos: Iterable[str],
    task_base: str | None = None,
) -> str | None:
    """Return the effective base branch for a repo or None for gh default.

    Precedence (most-specific wins):
        base_map entry > cli_base > task_base > repo_config.base_branch > None.

    `task_base` is the base pinned at spawn time via `--base` (Task.base_override,
    #42); it is None for ordinary tasks, so it never overrides a repo's configured
    base unless the operator explicitly stacked the task on another branch.
    Raises UnknownRepoInBaseMapError if base_map references a repo not in known_repos.
    """
    known = set(known_repos)
    unknown = [r for r in base_map if r not in known]
    if unknown:
        raise UnknownRepoInBaseMapError(
            f"Unknown repo(s) in --base-map: {', '.join(sorted(unknown))}. "
            f"Known: {sorted(known)}"
        )
    if repo_name in base_map:
        return base_map[repo_name]
    if cli_base is not None:
        return cli_base
    if task_base is not None:
        return task_base
    return getattr(repo_config, "base_branch", None)
