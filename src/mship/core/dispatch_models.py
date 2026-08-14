"""Resolve the model a dispatched subagent runs on (spec mship-dispatch-v2).

The dispatcher — not the untrusted worker — owns the model choice. Precedence:
CLI flag > `dispatch_models:` per-mode map in mothership.yaml > built-in
per-mode default.

`inherit` is a portable sentinel: the controller omits a model selector and
lets the harness use its current/default model. Explicit CLI/config values
are opaque operator choices passed through unchanged.
"""
from __future__ import annotations

BUILTIN_MODEL_DEFAULTS: dict[str, str] = {
    "implementer": "inherit",
    "standalone": "inherit",
    "reviewer": "inherit",
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
