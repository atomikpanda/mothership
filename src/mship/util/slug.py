import re

MAX_SLUG_LEN = 40
_PHRASE_SEPARATORS = ("—", ": ", ". ")
_ISSUE_ID_RE = re.compile(r"[a-z]+-\d+")


def slugify(text: str) -> str:
    """Convert a task description into a branch-safe slug.

    Applies a first-phrase heuristic (split on the earliest em-dash,
    colon-space, or period-space) before slugifying, then truncates the
    result to MAX_SLUG_LEN at a word boundary. Version numbers like
    `v1.0` and URL-like strings are preserved because the heuristic
    requires whitespace after `:` and `.`.

    Issue IDs (e.g. MOS-170) that fall past the truncation point are
    re-appended so they survive in branch names and auto-link in Linear.
    """
    cut = -1
    for sep in _PHRASE_SEPARATORS:
        i = text.find(sep)
        if i != -1 and (cut == -1 or i < cut):
            cut = i
    if cut != -1:
        text = text[:cut]
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    text = text.strip("-")
    if len(text) > MAX_SLUG_LEN:
        full_slug = text
        boundary = text.rfind("-", 0, MAX_SLUG_LEN + 1)
        text = text[:boundary] if boundary > 0 else text[:MAX_SLUG_LEN]
        text = text.rstrip("-")
        # Append any issue IDs that got truncated off.
        full_ids = _ISSUE_ID_RE.findall(full_slug)
        kept_ids = set(_ISSUE_ID_RE.findall(text))
        missing = [i for i in full_ids if i not in kept_ids]
        if missing:
            text = text + "-" + "-".join(missing)
    return text
