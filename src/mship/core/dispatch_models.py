"""Resolve the model a dispatched subagent runs on (spec mship-dispatch-v2).

The dispatcher — not the untrusted worker — owns the model choice. Precedence:
CLI flag > `dispatch_models:` per-mode map in mothership.yaml > built-in
per-mode default. Values are operator-chosen strings passed through verbatim
(harness-specific tier names are not validated here).

Upstream superpowers found that an unnamed model silently inherits the
session's most expensive tier; resolving here makes the choice explicit,
auditable, and configured in one place.
"""
from __future__ import annotations

# Reviewer work is judgment over prepared files — a cheaper tier by default.
# "inherit" means: no explicit model; the dispatching harness uses its session
# model. Implementers default to inherit so a capable session stays capable.
BUILTIN_MODEL_DEFAULTS: dict[str, str] = {
    "implementer": "inherit",
    "standalone": "inherit",
    "reviewer": "sonnet",
}


def resolve_model(mode: str, *, flag: str | None, configured: dict[str, str] | None) -> str:
    if mode not in BUILTIN_MODEL_DEFAULTS:
        raise ValueError(
            f"unknown dispatch mode {mode!r}; choose one of {', '.join(BUILTIN_MODEL_DEFAULTS)}"
        )
    if flag is not None:
        return flag
    if configured and mode in configured:
        return configured[mode]
    return BUILTIN_MODEL_DEFAULTS[mode]
