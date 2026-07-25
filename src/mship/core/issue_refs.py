"""GitHub issue references: extraction from free text, and canonical
'owner/repo#N' parsing for explicitly-linked tracker issues (#386)."""
from __future__ import annotations

import re
from typing import Iterable


_ISSUE_REF = re.compile(r"(?<![A-Za-z0-9_#])#(\d+)\b")


class IssueRefError(ValueError):
    pass


_SLUG_FORM = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")
_URL_FORM = re.compile(
    r"^https?://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)(?:[/?#].*)?$"
)
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


def append_linked_closes(body: str, canonical_refs: list[str], repo_slug: str | None) -> str:
    """Append Closes lines for WorkItem-linked issues: same-repo refs as
    'Closes #N', cross-repo as 'Closes owner/repo#N'. Skips refs whose closing
    line is already present in `body` (e.g. from the commit-derived footer).
    No-op when there is nothing new to add."""
    lines = []
    for canonical in canonical_refs:
        slug, num = issue_slug_and_number(canonical)
        line = f"Closes #{num}" if slug == repo_slug else f"Closes {canonical}"
        if line not in body and line not in lines:
            lines.append(line)
    if not lines:
        return body
    separator = "" if body.endswith("\n\n") else ("\n" if body.endswith("\n") else "\n\n")
    return body + separator + "\n".join(lines)


def extract_issue_refs(texts: Iterable[str]) -> list[int]:
    """Return unique, ascending-sorted issue numbers referenced across `texts`.

    Matches `#N` where N is one or more digits, not preceded by an identifier
    character. Handles `(#3)`, `, #3`, `Closes #3`, but NOT `abc#3` (anchor-link
    style) or `##3` (escaped markdown heading). Empty input returns `[]`.
    """
    found: set[int] = set()
    for t in texts:
        if not t:
            continue
        for match in _ISSUE_REF.finditer(t):
            try:
                found.add(int(match.group(1)))
            except ValueError:
                continue
    return sorted(found)


def append_closes_footer(body: str, refs: list[int]) -> str:
    """Append a `Closes #A, #B` footer to `body`. No-op when `refs` is empty.

    Uses `Closes` for output consistency (GitHub also accepts `Fixes`/`Resolves`).
    """
    if not refs:
        return body
    refs_str = ", ".join(f"#{n}" for n in refs)
    separator = "" if body.endswith("\n\n") else ("\n" if body.endswith("\n") else "\n\n")
    if separator == "\n":
        separator = "\n"  # keep one newline; we'll add another below
        return f"{body}\nCloses {refs_str}"
    if separator == "":
        return f"{body}Closes {refs_str}"
    return f"{body}{separator}Closes {refs_str}"
