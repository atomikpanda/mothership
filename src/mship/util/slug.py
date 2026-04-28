import re

MAX_SLUG_LEN = 40
_PHRASE_SEPARATORS = ("—", ": ", ". ")


def slugify(text: str) -> str:
    """Convert a task description into a branch-safe slug.

    Applies a first-phrase heuristic (split on the earliest em-dash,
    colon-space, or period-space) before slugifying, then truncates the
    result to MAX_SLUG_LEN at a word boundary. Version numbers like
    `v1.0` and URL-like strings are preserved because the heuristic
    requires whitespace after `:` and `.`.
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
        boundary = text.rfind("-", 0, MAX_SLUG_LEN + 1)
        text = text[:boundary] if boundary > 0 else text[:MAX_SLUG_LEN]
        text = text.rstrip("-")
    return text
