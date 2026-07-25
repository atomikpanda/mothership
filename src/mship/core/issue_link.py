"""Link GitHub tracker issues to WorkItems (#386).

Shared by `mship item link-issue`, `mship spawn --closes`, and
`mship spec dispatch --closes`.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from mship.core.issue_refs import issue_url, normalize_issue_ref
from mship.core.workitem import ExternalLink


def slug_for_path(path) -> str | None:
    """The 'owner/repo' a checkout's origin points at, or None."""
    from mship.core.pr import _parse_github_slug

    r = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    parsed = _parse_github_slug(r.stdout.strip())
    return f"{parsed[0]}/{parsed[1]}" if parsed else None


def default_issue_slug(repos) -> str | None:
    """The single 'owner/repo' every configured repo's origin points at, or
    None when zero or several distinct slugs resolve (caller must be explicit)."""
    slugs = {s for s in (slug_for_path(repo.path) for repo in repos) if s}
    return slugs.pop() if len(slugs) == 1 else None


def link_issue_to_item(items, item_id: str, ref: str, *, default_slug: str | None,
                       now: datetime | None = None) -> tuple[str, bool]:
    """Normalize `ref` and link it to the WorkItem's external_links.

    Returns (canonical, newly_linked); (canonical, False) means it was already
    linked. Raises IssueRefError on an invalid ref — callers surface that at
    the CLI boundary before any side effects.
    """
    canonical = normalize_issue_ref(ref, default_slug=default_slug)
    url = issue_url(canonical)
    item = items.get(item_id)
    if item is None:
        raise KeyError(f"WorkItem {item_id!r} not found; cannot link issue {canonical}")
    if any(link.url == url for link in item.external_links):
        return canonical, False
    items.add_external_link(item_id, ExternalLink(provider="github", url=url, title=canonical),
                            now=now or datetime.now(timezone.utc))
    return canonical, True


def linked_issue_refs(item) -> list[str]:
    """Canonical 'owner/repo#N' refs of the GitHub issues linked to a WorkItem."""
    return [link.title for link in item.external_links
            if link.provider == "github" and "/issues/" in link.url]
