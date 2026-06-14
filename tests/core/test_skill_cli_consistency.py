"""Systematic guard: every `mship <cmd>` or `mship <group> <sub>` in a bundled
skill markdown must correspond to a real command or subcommand in the live
Typer app.

Flag existence is intentionally not validated (too many doc-shorthand forms —
pipes in flag values, placeholder syntax, etc. would produce false positives).

## What is validated
- Top-level commands: each first path token must be a real command name.
- Subcommands (group commands): if the first token is a group (i.e., it has
  sub-commands), the second token (if present and looks like a command word) is
  validated against that group's known subcommands.  Pipe-alternation tokens
  (e.g. ``status|journal|diff|spec``) are split and each alternative is checked.
- Positional arguments: if the first token is a *leaf* command (not a group),
  the second token is treated as a positional arg — not validated.  This
  correctly handles ``mship phase dev``, ``mship switch my-repo``, etc.

## What counts as an invocation
Only these two forms are matched (to avoid false-positives in prose):
1. Inline backtick: `` `mship …` ``
2. A line inside a code-fence that starts with ``mship `` (optionally ``$ mship ``).

## Token extraction rules
After ``mship``, path tokens are collected while they match ``^[a-z][a-z-]*[a-z]$``
(at least 3 chars, lowercase letters and hyphens, no leading/trailing hyphen).
Collection stops at the first token that does NOT match (flags, ``<placeholder>``,
``[optional]``, quoted strings, jq pipes, etc.).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import pytest

# ---------------------------------------------------------------------------
# CLI introspection
# ---------------------------------------------------------------------------

def cli_tree() -> tuple[set[str], set[str], dict[str, set[str]]]:
    """Return (commands, groups, subcommands).

    commands    — all top-level command names (incl. group names)
    groups      — subset of commands that ARE groups (have children)
    subcommands — {group_name: set(child_command_names)}
    """
    import typer.main
    from mship.cli import app as _mship_app

    click_cmd = typer.main.get_command(_mship_app)
    commands: set[str] = set()
    groups: set[str] = set()
    subcommands: dict[str, set[str]] = {}
    for name, sub in getattr(click_cmd, "commands", {}).items():
        commands.add(name)
        kids = getattr(sub, "commands", {})
        if kids:
            groups.add(name)
            subcommands[name] = set(kids.keys())
    return commands, groups, subcommands


# ---------------------------------------------------------------------------
# Invocation extraction
# ---------------------------------------------------------------------------

# A "command word": lowercase letters and hyphens, at least 3 chars,
# no leading or trailing hyphen.
_CMD_WORD = re.compile(r"^[a-z][a-z-]{1,}[a-z]$")

# Matches inline backtick form: `mship …`
_BACKTICK_RE = re.compile(r"`(mship [^`]+)`")

# Matches code-fence form at start of line (optionally prefixed with "$ ")
_FENCE_LINE_RE = re.compile(r"(?m)^[ \t]{0,4}(?:\$ )?(mship \S.*?)(?:\s*#.*)?$")


class InvocationRef(NamedTuple):
    file: Path
    line_no: int
    raw: str       # the full matched invocation string
    tokens: list[str]  # extracted path tokens after 'mship'


def _extract_path_tokens(invocation_text: str) -> list[str]:
    """Extract leading command-path tokens from the text after 'mship '.

    Stops at the first token that is not a pure command word.
    Pipe-alternation tokens (``a|b|c``) are kept as a single token and handled
    by the caller.
    """
    parts = invocation_text.split()
    # parts[0] == 'mship'
    tokens: list[str] = []
    for tok in parts[1:]:
        # Strip surrounding punctuation that docs sometimes add
        tok = tok.strip("`[]().,;:")
        if not tok:
            continue
        # Allow pipe-alternation tokens like "status|journal|diff|spec"
        # as a single multi-part token — check each alternative
        alts = tok.split("|")
        # Each alternative must look like a command word for us to keep this token
        all_cmd_words = all(_CMD_WORD.match(a) for a in alts if a)
        if all_cmd_words and alts:
            tokens.append(tok)
        else:
            # Not a command word — this is a flag, placeholder, arg value, etc.
            break
    return tokens


def _is_in_code_fence(text: str) -> list[tuple[int, str]]:
    """Return list of (line_number_1based, stripped_line) for lines inside code
    fences that start with 'mship ' (optionally '$ mship ')."""
    results: list[tuple[int, str]] = []
    in_fence = False
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            # Line is inside a code fence
            cmd_line = stripped
            if cmd_line.startswith("$ "):
                cmd_line = cmd_line[2:]
            if cmd_line.startswith("mship "):
                # Strip trailing comment
                cmd_line = re.sub(r"\s+#.*$", "", cmd_line).rstrip()
                results.append((i, cmd_line))
    return results


def extract_invocations(md_path: Path) -> list[InvocationRef]:
    """Extract all mship invocations from a markdown file."""
    text = md_path.read_text(encoding="utf-8")
    found: list[InvocationRef] = []
    seen: set[str] = set()

    # 1. Inline backtick form
    for m in _BACKTICK_RE.finditer(text):
        raw = m.group(1)
        # Compute approximate line number
        line_no = text[: m.start()].count("\n") + 1
        tokens = _extract_path_tokens(raw)
        key = f"bt:{line_no}:{raw}"
        if key not in seen:
            seen.add(key)
            found.append(InvocationRef(file=md_path, line_no=line_no, raw=raw, tokens=tokens))

    # 2. Code-fence form
    for line_no, raw in _is_in_code_fence(text):
        tokens = _extract_path_tokens(raw)
        key = f"cf:{line_no}:{raw}"
        if key not in seen:
            seen.add(key)
            found.append(InvocationRef(file=md_path, line_no=line_no, raw=raw, tokens=tokens))

    return found


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationError(NamedTuple):
    ref: InvocationRef
    message: str


def validate_invocation(
    ref: InvocationRef,
    commands: set[str],
    groups: set[str],
    subcommands: dict[str, set[str]],
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    tokens = ref.tokens

    if not tokens:
        return errors  # empty — nothing to validate

    first = tokens[0]
    # Pipe-alternation at the first position is unusual but handle it
    first_alts = first.split("|")

    for first_tok in first_alts:
        if first_tok not in commands:
            errors.append(
                ValidationError(
                    ref=ref,
                    message=f"unknown top-level command {first_tok!r}",
                )
            )
            continue

        # First token is valid.  Now check the second if present.
        if len(tokens) < 2:
            continue

        second = tokens[1]
        second_alts = second.split("|")

        if first_tok not in groups:
            # Leaf command: the second token is a positional arg, not a subcommand.
            # Do not validate it.
            continue

        # Group command: validate each alternative in the second token.
        known_subs = subcommands.get(first_tok, set())
        for sub_tok in second_alts:
            if sub_tok and sub_tok not in known_subs:
                errors.append(
                    ValidationError(
                        ref=ref,
                        message=(
                            f"unknown subcommand {first_tok!r} {sub_tok!r}"
                            f" (known: {sorted(known_subs)!r})"
                        ),
                    )
                )

    return errors


# ---------------------------------------------------------------------------
# Real-skills integration test
# ---------------------------------------------------------------------------

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src" / "mship" / "skills"


def test_all_skill_commands_exist_in_cli():
    """Every mship invocation in any bundled skill markdown must reference a
    real command/subcommand in the live Typer app."""
    commands, groups, subcommands = cli_tree()

    all_errors: list[ValidationError] = []
    md_files = sorted(SKILLS_DIR.rglob("*.md"))
    assert md_files, "No skill markdown files found — SKILLS_DIR may be wrong"

    for md_path in md_files:
        invocations = extract_invocations(md_path)
        for ref in invocations:
            all_errors.extend(validate_invocation(ref, commands, groups, subcommands))

    if all_errors:
        lines = []
        for err in all_errors:
            rel = err.ref.file.relative_to(SKILLS_DIR)
            lines.append(
                f"  {rel}:{err.ref.line_no}: {err.message}\n"
                f"    invocation: {err.ref.raw!r}"
            )
        pytest.fail(
            f"{len(all_errors)} invalid mship reference(s) in bundled skills:\n"
            + "\n".join(lines)
        )


# ---------------------------------------------------------------------------
# Matcher unit tests
# ---------------------------------------------------------------------------

def _validate_text(text: str) -> list[ValidationError]:
    """Helper: extract invocations from a markdown text string and validate them."""
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
        f.write(text)
        tmp = Path(f.name)
    try:
        commands, groups, subcommands = cli_tree()
        refs = extract_invocations(tmp)
        errors: list[ValidationError] = []
        for ref in refs:
            errors.extend(validate_invocation(ref, commands, groups, subcommands))
        return errors
    finally:
        os.unlink(tmp)


def test_matcher_spec_verdict_pipe_values_ignored():
    """``mship spec verdict <id> <crit> approved|flagged``: ``approved|flagged``
    comes after a non-command token (``<id>``), so stops token collection at
    ``<id>``.  The ``approved|flagged`` part must never be treated as a
    subcommand alternative and must NOT raise an error."""
    text = "Use `mship spec verdict <id> <crit> approved|flagged` to mark a criterion.\n"
    errors = _validate_text(text)
    assert not errors, f"Unexpected errors: {errors}"


def test_matcher_phase_leaf_positional_arg():
    """``mship phase dev``: ``phase`` is a leaf command; ``dev`` is a positional
    arg.  Must NOT be flagged as an unknown subcommand."""
    text = "Run `mship phase dev` to advance to the dev phase.\n"
    errors = _validate_text(text)
    assert not errors, f"Unexpected errors: {errors}"


def test_matcher_view_pipe_alternation_subcommands():
    """``mship view status|journal|diff|spec``: ``view`` IS a group; each of
    ``status``, ``journal``, ``diff``, ``spec`` is a real subcommand.  Must
    pass with zero errors."""
    text = """
```bash
mship view status|journal|diff|spec [--watch]
```
"""
    errors = _validate_text(text)
    assert not errors, f"Unexpected errors: {errors}"


def test_matcher_rejects_bogus_top_level_command():
    """A made-up top-level command must be flagged."""
    text = "Use `mship frobnicate` to do the thing.\n"
    errors = _validate_text(text)
    assert any("frobnicate" in e.message for e in errors), (
        f"Expected 'frobnicate' to be flagged, got: {errors}"
    )


def test_matcher_rejects_bogus_subcommand():
    """A real group with a made-up subcommand must be flagged."""
    text = "Use `mship spec frobnicate` to do the thing.\n"
    errors = _validate_text(text)
    assert any("frobnicate" in e.message for e in errors), (
        f"Expected 'spec frobnicate' to be flagged, got: {errors}"
    )
