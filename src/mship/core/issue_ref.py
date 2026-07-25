"""Normalize GitHub issue references to canonical 'owner/repo#N' form (#386)."""
from __future__ import annotations

import re


class IssueRefError(ValueError):
    pass


_SLUG_FORM = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")
_URL_FORM = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)/?$")
_NUM_FORM = re.compile(r"^#?(\d+)$")


def normalize_issue_ref(ref: str, *, default_slug: str | None) -> str:
    """Return 'owner/repo#N' for any accepted form: owner/repo#N, a full GitHub
    issue URL, '#N', or a bare number (the latter two need `default_slug`)."""
    ref = ref.strip()
    if m := _SLUG_FORM.match(ref):
        return f"{m.group(1)}/{m.group(2)}#{int(m.group(3))}"
    if m := _URL_FORM.match(ref):
        return f"{m.group(1)}/{m.group(2)}#{int(m.group(3))}"
    if m := _NUM_FORM.match(ref):
        if not default_slug:
            raise IssueRefError(
                f"issue ref {ref!r} has no repo; use the owner/repo#N form "
                f"(no unambiguous default repo could be resolved)"
            )
        return f"{default_slug}#{int(m.group(1))}"
    raise IssueRefError(
        f"unrecognized issue ref {ref!r}; use #N, N, owner/repo#N, or an issue URL"
    )


def issue_url(canonical: str) -> str:
    slug, num = canonical.rsplit("#", 1)
    return f"https://github.com/{slug}/issues/{num}"


def issue_slug_and_number(canonical: str) -> tuple[str, int]:
    slug, num = canonical.rsplit("#", 1)
    return slug, int(num)
