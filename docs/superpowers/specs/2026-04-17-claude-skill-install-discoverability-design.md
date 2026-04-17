# Claude Code skill install discoverability — Design

## Context

`mship skill install` is supposed to make mship's bundled skills (`working-with-mothership`, `brainstorming`, `writing-plans`, etc.) discoverable to whichever AI agent the user has installed. For Codex it works: `mship` clones the source to `~/.codex/mothership/` and symlinks `~/.agents/skills/mothership` into it; Codex auto-discovers from `~/.agents/skills/` (source-confirmed in `openai/codex/codex-rs/core-skills/src/loader.rs`).

For Claude Code it doesn't. The current `_install_claude` just prints two slash commands (`/plugin marketplace add ...` and `/plugin install ...`) and asks the user to paste them into the REPL. If the user doesn't run them — and most don't — Claude Code's `Skill` tool sees zero mothership skills, the brainstorming/writing-plans flow can't be invoked, and an agent working in a mothership workspace silently bypasses the prescribed methodology.

This was discovered concretely: an agent shipped two PRs against issue #50 without ever invoking `brainstorming` because the skills weren't in its available-skills list.

## Goal

Make mship-installed skills automatically discoverable by Claude Code on next session start, with no REPL slash commands or other manual user action. Maintain the existing Codex install path (with one cleanup). Lock skill content to mship CLI version by construction.

## Success criterion

After running `mship skill install` once on a machine where `claude` is on PATH (or `~/.claude/` exists), the user opens any new Claude Code session in any directory and the model's available-skills list — surfaced via the system-reminder Claude Code injects at session start — includes every mothership skill (`working-with-mothership`, `brainstorming`, `writing-plans`, `executing-plans`, etc.) with their bare names. No Anthropic plugin marketplace involvement; no `~/.claude/settings.json` edits.

## Anti-goals

- No project-scope install (`<workspace>/.claude/skills/`) in this PR — known-future option, deferred.
- No `mship doctor` *auto-fix* for missing skills; doctor diagnoses and points at remediation only.
- No per-skill version pinning. Skills version *is* mship version after this change, by construction.

## Architecture

### Source of truth

Skills live inside the mship Python package: `src/mship/skills/<name>/SKILL.md`. Built into the wheel as package data. Resolved at runtime via:

```python
from pathlib import Path
import mship
SKILLS_SOURCE = Path(mship.__file__).parent / "skills"
```

For `uv tool install mothership` users this is `~/.local/share/uv/tools/mothership/lib/python3.x/site-packages/mship/skills/`; for editable/pip installs it's the venv equivalent.

### Per-agent discovery surface

Symlinks from each agent's discovery dir into the package source:

| Agent | Symlink | Why |
|---|---|---|
| Codex | `~/.agents/skills/mothership` → `<pkg>/skills/` (1 symlink, dir-level) | Codex scans `~/.agents/skills/*/SKILL.md` recursively |
| Claude Code | `~/.claude/skills/<name>` → `<pkg>/skills/<name>/` (N symlinks, one per skill) | Claude scans `~/.claude/skills/<name>/SKILL.md` (one skill per immediate child) |

The asymmetry is forced by Claude's discovery shape (one skill per top-level child of `~/.claude/skills/`); Codex tolerates a deeper layout under one symlink.

### Why this works for live updates

`uv tool upgrade mothership` rebuilds the venv in place at the same path. Existing symlinks in `~/.claude/skills/<name>` and `~/.agents/skills/mothership` keep resolving and pick up the new content automatically. **The implementation plan must include a smoke test confirming this assumption** before the PR can merge.

If the assumption ever fails (e.g., a future uv version changes paths), the doctor check (Section: Doctor) will surface the dangling symlinks and the user runs `mship skill install` to refresh.

## `mship skill install` behavior

### Steady-state flow (no flags)

1. `SKILLS_SOURCE = Path(mship.__file__).parent / "skills"`
2. Detect agents via existing `_detect_agents()` (claude/codex/gemini on PATH or `~/.<agent>/` exists).
3. For each detected agent, refresh that agent's symlinks per the collision table below.
4. Print a one-line summary per agent:
   ```
   claude: 11 skills installed → ~/.claude/skills/
   codex:  11 skills installed → ~/.agents/skills/mothership/
   gemini: not detected (skip)
   ```

### Collision handling per target symlink path

A symlink is considered **owned by mship** if it resolves into either:
- the current package source (`<pkg>/skills/`), or
- a known-historical source path that mship used in earlier versions. Initial value: `~/.codex/mothership/skills/`. The list is a constant in the install module so old layouts can be transparently migrated as the source path evolves.

A **broken symlink** (target doesn't exist on disk) is treated as owned by mship if its *intended* target path is in the owned-set above — i.e., we'd have created it ourselves. Otherwise it's foreign.

| Existing entry at target | Action without `--force` | Action with `--force` |
|---|---|---|
| Doesn't exist | Create symlink | Create symlink |
| Symlink → resolves into owned source (current or historical) | Replace (idempotent refresh) | Replace |
| Broken symlink → intended target was an owned source | Replace (transparent migration) | Replace |
| Symlink → resolves elsewhere | Skip + warn | Replace, log loudly |
| Broken symlink → intended target is foreign | Skip + warn | Replace, log loudly |
| Real file or directory | Skip + warn | Remove + replace, log loudly |

### Flag surface

| Flag | Behavior |
|---|---|
| `--only claude,codex,gemini` | Limit to listed agents instead of auto-detect |
| `--force` | Override collision safe-skip |
| `--yes` / `-y` | Skip the per-agent confirmation prompt (existing behavior) |

### Removed surface

- `--all` flag (installing all skills is now the default; specifying nothing means "all").
- `mship skill install <name>` (single-skill install). Source is bundled in the package; cherry-picking one skill out doesn't compose with the auto-update story. Help text guides users who want to limit Claude install to a subset to `rm ~/.claude/skills/<other>` post-install.
- `--dest PATH` (no longer meaningful: source is the package, can't be relocated).
- `_install_claude()`'s "print slash commands" path. Anyone who explicitly wants the plugin marketplace install can run `/plugin install` themselves; mship doesn't surface it.

### Output format

- Human (TTY): one-line summary per agent as above.
- JSON (non-TTY): `{"installed": [{"agent": "claude", "count": 11, "dest": "~/.claude/skills/", "skipped": ["<skill-name>", ...], "replaced": ["<skill-name>", ...]}, ...]}`. `count` is the number of symlinks now in place; `skipped` lists skill names where collision safe-skip fired (target had foreign content); `replaced` lists skill names where an existing owned-or-historical symlink was refreshed.

## Migration / cleanup

No state migration of stored content (the package-bundled architecture eliminates the need). Old layout cleanup is best-effort:

```
On first run of new `mship skill install`:
  - If ~/.codex/mothership/ exists (old git clone source):
      print: "old skills source `~/.codex/mothership/` no longer used;
              safe to `rm -rf` it"
      Don't auto-delete — that's user state.
  - Refresh symlinks per the collision table above.
    The pre-existing `~/.agents/skills/mothership` symlink that pointed into
    `~/.codex/mothership/skills/` is now functionally dangling; the symlink-
    refresh treats it as "ours" (it once resolved into something we managed)
    and replaces it transparently.
```

Plugin-installed skills (if a user previously ran `/plugin install mothership@mothership-marketplace`) live under `~/.claude/plugins/cache/`, not `~/.claude/skills/` — unaffected by this migration. They'll coexist with the new user-scope install (plugin-namespaced `mothership:working-with-mothership` vs bare `working-with-mothership`); both work; no conflict.

## `mship doctor` skill-availability check

Add a check that diagnoses skill discoverability for each detected agent:

```
For each detected agent (claude, codex, gemini):
  Determine expected install location.
  For each skill in <pkg>/skills/:
    Verify a symlink/dir exists at the expected location.
    Verify the symlink resolves and points at the current pkg source.
  Report one of:
    "claude: 11/11 skills installed and current"                          ✓
    "claude: 0/11 skills installed — run `mship skill install`"           ⚠
    "claude: 5/11 skills installed (6 missing) — run `mship skill install`"  ⚠
    "claude: 11/11 installed, 3 dangling — run `mship skill install`"     ⚠
    "claude: 8/11 installed, 2 dangling, 1 foreign (skipped) — see install --force"  ⚠
```

No auto-fix. Diagnose and remediate.

## Testing strategy

- **Unit:** symlink refresh function — collision matrix as a test per row; `_detect_agents` mocked; doctor check's diagnostic output.
- **Integration:** real install into a `tmp_path` `HOME` — verify `~/.claude/skills/<name>/SKILL.md` is reachable through the symlink and content matches package source; verify Codex symlink shape.
- **Migration:** seed the old `~/.codex/mothership/` layout in tmp `HOME`, run install, verify no errors and the deprecation message prints.
- **Smoke (manual, called out in the plan):** real shell — `uv tool install mothership` → `mship skill install` → `claude` → confirm `working-with-mothership` appears in available-skills. Then `uv tool upgrade mothership` → re-launch claude → confirm skills resolve and reflect updated content. **This is the gate that validates the "live updates via symlinks survive uv upgrade" assumption from the architecture section.**
- **Existing tests:** `tests/cli/test_skill.py` rewrites — install model changes substantially.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Install Claude skills at user scope (`~/.claude/skills/<name>/`) | Matches Codex install model; user-scope is correct ownership for mship-version-coupled content; project scope is a future opt-in. |
| 2 | (Superseded) Use neutral source path `$XDG_DATA_HOME/mship/skills/` with shared symlinks | Original choice; superseded by decision 5. |
| 3 | Detect "is it ours" for collision handling: replace if symlink resolves into our source, safe-skip otherwise | Idempotent re-installs without flags; never destroys user state silently. |
| 4 | `mship skill install` defaults to file-write only; remove the slash-command output entirely | The slash-command path was the failure mode this PR fixes; users who want plugin-managed install can run `/plugin install` themselves with no mship involvement. |
| 5 | **Bundle skills inside the mship Python package** instead of a separate git clone | Eliminates skill/CLI version drift by construction; removes XDG source dir, `git clone`/`git pull` machinery, and the `~/.codex/mothership/` migration burden. Skill edits flow through the mship release process where they're tested against the CLI features they reference. |
