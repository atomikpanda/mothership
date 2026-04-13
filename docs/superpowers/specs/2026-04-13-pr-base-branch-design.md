# Per-Repo PR Base Branch

**Status:** Design approved, ready for implementation plan.
**Date:** 2026-04-13

## Purpose

`mship finish` currently creates PRs without passing `--base`, so `gh pr create` uses the remote default branch. This breaks workflows where individual repos target a non-default base (e.g. one repo merges into `cli-refactor`, another into `main`). This feature adds a configurable per-repo base branch plus two CLI overrides, with up-front verification that each base exists remotely before any push.

## Non-Goals

- Cross-repo base branch coordination (e.g. "all repos target whatever `schemas` targets"). Out of scope.
- Stacked/nested PRs. Out of scope.
- Creating the base branch if missing. `mship` only verifies; it does not create.
- Workspace-level default base branch. Explicitly rejected in brainstorming — unconfigured repos continue to use gh's default, which preserves today's behavior.

## Config Surface

New optional field on `RepoConfig` in `src/mship/core/config.py`:

```yaml
repos:
  cli:
    path: ../cli
    base_branch: main          # optional
  api:
    path: ../api
    base_branch: cli-refactor
  schemas:
    path: ../schemas           # no base_branch → gh default
```

Field type: `str | None`, default `None`. When `None`, `mship finish` does not pass `--base` to `gh pr create` (identical to today's behavior).

## CLI Additions

Two new options on `mship finish`:

- `--base <branch>` — global override applied to every affected repo.
- `--base-map <repo>=<branch>[,<repo>=<branch>…]` — selective per-repo overrides.

### Precedence (most-specific wins)

For each repo, the effective base is determined by the first rule that matches:

1. `--base-map` entry for this repo name.
2. `--base` global flag.
3. `repo.base_branch` from config.
4. `None` → `gh pr create` uses the remote default branch.

Rationale: `--base-map` existing alongside `--base` only makes sense if the map overrides the flag. The ordering matches the standard "most specific wins" mental model (CSS, env layering).

### `--base-map` parsing

- Comma-separated `repo=branch` pairs.
- Whitespace around `=` and `,` is tolerated.
- Unknown format (missing `=`, empty key, empty value) → exit 1 with a hint showing the expected shape.
- Any repo name not present in the workspace config → exit 1 listing the offending names. Check runs before any network call.

## Remote Verification (fail-fast)

Before any `git push` or `gh pr create`:

1. Compute the effective base for every affected repo (per the precedence above).
2. For each repo with a non-`None` effective base, run `git ls-remote --heads origin <base>` in the repo's path.
3. Collect all repos where the command returns no matching ref (missing) or fails (network/auth error — treated as missing).
4. If any failures, exit 1 with a grouped report. No repos are pushed.

Runs sequentially — typical workspace has 2–5 repos; network latency dominated by repo count, total under a second. Parallelization deferred until measured to matter.

Repos whose effective base is `None` skip verification (there's no base to check; gh will pick the default at PR creation time).

## `PRManager` Changes

File: `src/mship/core/pr.py`.

- `create_pr(..., base: str | None = None)` — new optional kwarg. When set, append ` --base <shlex.quote(base)>` to the `gh pr create` command. No other changes.
- `verify_base_exists(repo_path: Path, base: str) -> bool` — new method. Wraps `git ls-remote --heads origin <base>`. Returns `True` if the output contains a ref; `False` on empty output or non-zero exit.

## Output

### TTY mode

Live per-repo line during the PR loop:

```
cli: feat/task-x → main  ✓ https://github.com/owner/cli/pull/42
api: feat/task-x → cli-refactor  ✓ https://github.com/owner/api/pull/43
schemas: feat/task-x → (default)  ✓ https://github.com/owner/schemas/pull/44
```

`(default)` is literal text shown when the effective base is `None`.

### JSON mode

Each entry in the `prs` list gains a `"base"` field:

```json
{
  "repo": "cli",
  "url": "https://github.com/owner/cli/pull/42",
  "order": 1,
  "base": "main"
}
```

`"base"` is `null` when the effective base is `None`.

## Architecture & File Layout

### Files touched

- **Modify** `src/mship/core/config.py` — add `base_branch: str | None = None` to `RepoConfig`.
- **Modify** `src/mship/core/pr.py` — add `base` kwarg to `create_pr`; add `verify_base_exists`.
- **Create** `src/mship/core/base_resolver.py` — pure functions:
  - `parse_base_map(raw: str) -> dict[str, str]`
  - `resolve_base(repo_name, repo_config, cli_base, base_map) -> str | None`
- **Modify** `src/mship/cli/worktree.py` — `finish` command gains `--base` / `--base-map` options; calls `resolve_base` per repo; calls `verify_base_exists` up front; passes `base=` to `create_pr`; prints the new per-repo line format.

### Tests

- **Create** `tests/core/test_base_resolver.py` — unit tests for both helpers (precedence cases, map parser edge cases, unknown-repo rejection).
- **Modify** `tests/core/test_pr.py` (or equivalent) — assert `create_pr` produces the right `gh` invocation with and without `base=`; assert `verify_base_exists` against a local bare-repo fixture.
- **Modify** `tests/test_finish_integration.py` — extend to cover a config with `base_branch` set; assert the ShellRunner fake receives `gh pr create … --base <branch>`.

## Error Handling

- Invalid `--base-map` syntax → exit 1 with: `Invalid --base-map format. Expected: repo=branch,repo=branch. Got: <raw>`.
- Unknown repo in `--base-map` → exit 1 with: `Unknown repo(s) in --base-map: <name,name>. Known: <list>`.
- Missing remote base → exit 1 with grouped report; zero state changes.
- `git ls-remote` network failure → counted as missing (fail-closed). Error message distinguishes "not found" vs "lookup failed" in the grouped report.
- Any error leaves existing state untouched — `task.pr_urls` only gets written after a successful `create_pr`, as today.

## Out of Scope (post-v1 candidates)

- Workspace-level `default_base_branch` fallback.
- Parallel `ls-remote` verification.
- Auto-creating the base branch if missing.
- `--base-map` from a file (`@path.yaml`).
