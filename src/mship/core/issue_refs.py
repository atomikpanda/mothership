"""Extract GitHub issue references (`#N`) from free text."""
from __future__ import annotations

import re
from typing import Iterable


_ISSUE_REF = re.compile(r"(?<![A-Za-z0-9_#])#(\d+)\b")


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
