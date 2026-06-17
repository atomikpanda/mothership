"""Resolve a workspace member's clone URL from its config + default_remote.

Pure, no I/O. Rules (see spec mship-bootstrap / MOS-180):
  - `url` with a scheme ("://") or a "git@" prefix -> used as-is (any host/SSH).
  - `url` as "owner/repo" (contains "/", no scheme) -> https://github.com/owner/repo.
    github.com is the assumed host for this shorthand; other hosts (and local
    paths) must use a full URL, e.g. file:///path or https://gitlab.com/g/r.
  - `url` as a bare "repo" (no "/") -> {default_remote}/repo.
  - `url` omitted -> {default_remote}/{member_name}.
  - bare/omitted with no default_remote -> None (caller emits a per-member error).
"""
from __future__ import annotations

from mship.core.config import RepoConfig

_GITHUB = "https://github.com"


def resolve_clone_url(
    name: str, repo: RepoConfig, default_remote: str | None
) -> str | None:
    base = default_remote.rstrip("/") if default_remote else None
    url = repo.url.strip() if repo.url else None

    if url:
        if "://" in url or url.startswith("git@"):
            return url
        if "/" in url:
            return f"{_GITHUB}/{url}"
        # bare repo name
        return f"{base}/{url}" if base else None

    # url omitted -> default_remote/member_name
    return f"{base}/{name}" if base else None
