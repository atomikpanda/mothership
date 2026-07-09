"""Read-time auto-linkify of native mship entity refs in message text.

Wraps exact tokens that match a live entity id/slug in a groundcontrol:// markdown
link. Protects existing markdown links, inline code, and fenced code blocks. Native
mship entities only (wi- ids, spec ids, task slugs) — no external refs.
"""
from __future__ import annotations

import re

# a run of alnum groups optionally joined by hyphens, matched on identifier
# boundaries so "mos-2240" is NOT split into "mos-224" + "0" (greedy quantifier
# eats the whole run, then a whole-token lookup either hits or misses). Zero
# hyphens is allowed so bare alnum slugs like "gc31" are still eligible tokens.
# `_` counts as an identifier char on both sides (leading lookbehind + trailing
# lookahead) so a ref glued into a snake_case name like "some_gc31_thing" is left
# untouched. Hyphen handling is unchanged.
_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9_])")
# KNOWN LIMITATION (do not fix): this is regex-based, not a real markdown parser, so
# unpaired single backticks and GFM double-backtick (``) code spans aren't perfectly protected.
_PROTECTED = re.compile(r"```.*?```|`[^`]*`|\[[^\]]*\]\([^)]*\)", re.DOTALL)


def _kind_for(token, item_ids, spec_ids, task_slugs):
    if token in item_ids:
        return "item"
    if token in spec_ids:
        return "spec"
    if token in task_slugs:
        return "task"
    return None


def _linkify_span(text, item_ids, spec_ids, task_slugs):
    def repl(m):
        tok = m.group(0)
        kind = _kind_for(tok, item_ids, spec_ids, task_slugs)
        return tok if kind is None else f"[{tok}](groundcontrol://{kind}?id={tok})"
    return _TOKEN.sub(repl, text)


def linkify_entities(text, item_ids, spec_ids, task_slugs):
    """Rewrite unprotected occurrences of known entity ids/slugs in `text` into
    groundcontrol:// markdown links, by precedence item > spec > task. Skips
    fenced code blocks, inline code, and text already inside a markdown link."""
    out, last = [], 0
    for m in _PROTECTED.finditer(text):
        out.append(_linkify_span(text[last:m.start()], item_ids, spec_ids, task_slugs))
        out.append(m.group(0))
        last = m.end()
    out.append(_linkify_span(text[last:], item_ids, spec_ids, task_slugs))
    return "".join(out)
