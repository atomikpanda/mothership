# Better CLI Help: Auto-help + List Available Options on Errors

**Status:** Design approved, ready for implementation.
**Date:** 2026-04-14

## Purpose

When a `mship` command requires an argument and gets none — or gets an invalid one — the error currently reads `Missing argument 'X'` or `Unknown repo: foo`. Neither tells the human or agent **what valid values exist**. mship knows the valid set (configured repos, known task slugs, available specs); it should always include them.

## Changes

1. **`no_args_is_help=True`** on the root `mship` Typer app and the `view` sub-app. `mship` alone and `mship view` alone show help instead of erroring.
2. **`_resolve_repos` error lists available repos.** `Unknown repo 'foo'. Available: api, shared, schemas.` — used by `test`, `run`, `audit`, `sync`.
3. **`mship logs <service>`** — missing or invalid service prints `Available services: <list>`.
4. **`mship view spec <name>`** — `SpecNotFoundError` for a named-but-missing spec lists up to 5 available specs in the directory (and `(N more)` if truncated).
5. **`mship view logs <task-slug>`** — invalid slug lists known task slugs from state.

## Non-Goals

- Auto-help when a positional arg is missing on a single command (Typer's default behavior is fine — users can `--help`).
- Polishing literal-typed arg errors (`mship phase plan|dev|review|run`). Typer's existing behavior is acceptable.
- Suggesting "did you mean X?" via fuzzy matching. Future work.

## Testing

One integration test per change asserting the available list appears in stderr/output.
