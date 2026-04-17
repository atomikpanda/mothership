# README refresh — Design

## Context

The current `README.md` is 461 lines. It has a strong value-prop paragraph, a dense failure catalog, a decent 60-second walkthrough, and then roughly 280 lines of inline reference content (CLI commands, configuration options, healthchecks, monorepo rules, task aliasing, Taskfile contract). It also contains concretely stale content from work that shipped this session: the skill-install section still describes the old `/plugin marketplace add` flow that PR #55 replaced, the CLI reference omits `mship context` and `mship dispatch`, and `mship finish`'s reference doesn't show `--body-file`, `--body`, or `--force` which PRs #56–#58 added.

This PR is a scoped refresh per a brief from the user that optimizes the README for a specific reader: a developer who might actually use the tool. The secondary reader (a hiring manager / FDE-track interviewer evaluating the author) is served as a consequence of serving the primary reader well, not by writing for them directly. The brief explicitly rules out marketing voice, feature lists before problem statements, decorative badges, roadmaps in the README, and adjectives the reader can't verify from the code.

## Goal

A reader who closes the README after 90 seconds can answer four questions:

1. What is it?
2. Who is it for?
3. How would I try it?
4. What does it not do?

If any of the four requires scrolling or inference, the draft has failed.

## Success criterion

- README fits on one screen's worth of scrolling for the parts that answer questions 1–4 (one-sentence description, problem, quickstart, scope).
- Quickstart is ≤15 lines of copy-pasteable shell with no unexplained placeholders.
- No stale content (skill-install, CLI commands, `finish` flags) once the PR lands.
- Deep reference content (full CLI, configuration surface) lives in `docs/cli.md` and `docs/configuration.md`, linked from the README.
- Prose rules enforced (see below); final pass cuts 20% of whatever survived the first draft.

## Anti-goals

Per the brief's explicit rules:

- **No marketing voice.** No "powerful," "seamless," "robust," "elegant," "blazingly fast." No adjectives that the quickstart doesn't demonstrate.
- **No feature lists before problem statements.** The problem comes first; features are what make the problem go away.
- **No emoji in headers.** No decorative badges (stars, "made with love," tech-stack logos).
- **No roadmap, no "this project aims to…", no apologies** about pre-1.0 status beyond a neutral one-line callout.
- **No GIF or screenshot above the quickstart.** Visual content delays the working path.
- **No reproducing GitHub chrome** (license badge, language breakdown, star count) — GitHub already shows it.

Also out of scope for this PR:

- No website / docs site / versioned docs. The user and the session agreed the project is not yet at that milestone.
- No PyPI publishing (separate prerequisite for landing-page-scale docs).
- No rewriting SKILL.md files or AGENTS.md. Those are scoped to their own audiences.
- No CHANGELOG.md creation — deferred.

## Architecture

### New README structure

Top-to-bottom, in the order readers see content:

1. **Title.** `# mship`
2. **One-sentence description.** *"State safety for an AI agent working across your git repos: isolated worktrees per task, coordinated PRs, durable cross-session state."*
3. **Status callout.** One line: `> Pre-1.0. API may change. Pin a commit if you need stability.`
4. **`## Problem`** — 2-4 sentences naming the specific friction (AI agents + multiple repos = state failures that git alone doesn't model). No enumerated bullet list; prose only.
5. **`## Quickstart`** — ≤15 lines of shell. Single-repo path (simplest case that exercises the full loop: init → spawn → cd → edit → commit → finish). `uv tool install git+github` as the install, `mship init --name X --repo .:service` as the config, `mship spawn`, `cd $(mship status | jq -r …)`, an edit + commit, `mship finish --body-file /tmp/body.md`. Every line copy-pasteable.
6. **`## How it works`** — 3–6 sentences on the mechanism: worktree per task per repo, state in `.mothership/state.yaml`, audit gates on transitions, pre-commit hook, structured state surfaced via `mship status`/`journal`/`context`.
7. **`## Common patterns`** — exactly two concrete patterns that answer "why would I reach for this?":
   - **Multi-repo task** (`mship spawn --repos …`, test in dep order, coordinated PRs)
   - **Agent session handoff** (`mship dispatch` emits self-contained prompt for a fresh subagent)
8. **`## Scope`** — three bullet rows: `Does:`, `Does not:`, `Works for:`. Each row is a single comma-separated list, not an expanded subsection. No `Doesn't work for:` row (decision logged).
9. **`## Reference`** — three bullets linking out to `docs/cli.md`, `docs/configuration.md`, and `mship skill install` for the AI-agent bundle.
10. **`## License`** — MIT.

Target length after the cut-20% pass: ≈50–60 lines.

### Move plan

Two new files under `docs/`:

#### `docs/cli.md`

Everything currently in the `## CLI Reference`, `### \`mship finish\``, `### Drift audit & sync`, and `### Live views` sections of the existing README (lines 183–284). Reorganized so the command groups stay logical:

- Lifecycle (the iteration loop)
- Inspection
- Maintenance
- Long-running services
- `mship finish` detail (PR base resolution, `--body` / `--body-file` / `--force`, etc.)
- Drift audit & sync (audit codes, per-repo + workspace policy)
- Live views

Also add the commands that are missing from the current reference: `mship context`, `mship dispatch`. Update `mship finish`'s line to show `--body-file`, `--body`, `--force` flags. Update `mship skill install` to reflect the post-PR-55 install model (writes directly to `~/.claude/skills/` and `~/.agents/skills/mothership/`; no REPL slash commands).

#### `docs/configuration.md`

Everything currently in the `## Configuration` section (lines 286–447). Reorganized into sub-sections that mirror their existing headings:

- `mothership.yaml` overview
- Secret management (`env_runner`)
- Monorepo support (`git_root`)
- Service start modes (`start_mode`)
- Healthchecks
- Task name aliasing
- Taskfile contract

No content changes — pure move. Exception: the existing `## How it fits` and `## Multi-task workflows` sections (lines 78–142) don't belong in the configuration doc; the former folds into the README's new `## How it works` section (compressed from ~10 lines to ~5), the latter folds into `docs/cli.md` as a small subsection under the resolution-rule coverage.

### Prose rules (enforced on every line)

- Active voice, present tense, short sentences.
- No adjectives that the code doesn't demonstrate.
- Every code block has a language hint (` ```bash `, ` ```yaml `, ` ```markdown `).
- No placeholder like `<your-thing-here>` in the quickstart unless genuinely unavoidable.
- Status, stability, and maturity stated neutrally — one line, no apology.
- Write for the developer who might use the tool. Never write for the hiring manager.

## The cut-20% pass

After the first draft passes the 90-second test on its own terms, do one more pass and cut 20% of the remaining lines. The brief's rule: "Whatever survives the cut is the README." Candidates for the cut:

- Redundant words in the Quickstart (e.g., `&& git commit` that could be two short lines).
- Adjectives that snuck back in ("simple," "just," "quickly").
- Second-level explanations in `## How it works` that restate something already visible in the Quickstart.
- Anything in `## Common patterns` that enumerates a feature rather than answering "why reach for this."

## Testing

No automated tests (README is prose). Manual verification:

1. **90-second test.** Close the file and time-box reading. At the 90-second mark, can I answer the four questions without scrolling? If not, the structure is wrong — escalate back to design rather than patching prose.
2. **Copy-paste test.** Run the Quickstart verbatim in a fresh scratch directory. Every command must work without manual substitution. This catches placeholder leaks and missing prerequisites.
3. **Markdown render check.** `uv run python -c "import mistune; mistune.html(open('README.md').read())"` parses cleanly; all links resolve to files in the repo.
4. **Reference doc sanity.** `docs/cli.md` and `docs/configuration.md` each render cleanly and cover every command / config key the removed sections did.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | One-sentence description: *"State safety for an AI agent working across your git repos: isolated worktrees per task, coordinated PRs, durable cross-session state."* | Names outcome first ("state safety"), audience second ("AI agent"), mechanism third (worktrees/coordination/state). Colon + three-item list reads as structure, not marketing. The user picked this over mechanism-forward and shortest-essence alternatives. |
| 2 | Scope section includes `Does:`, `Does not:`, `Works for:` — no `Doesn't work for:` row | The "doesn't work for" edge (shallow clones, Dockerized runners without worktree support) is niche; readers who hit it discover it from the first `mship spawn` error. Per the brief's "when in doubt, cut," leaving the row out preserves the section's signal-to-noise ratio. |
| 3 | Quickstart is single-repo, not multi-repo | Minimum complete loop. Multi-repo is the scaling story, covered in `## Common patterns`. The brief caps the Quickstart at ~15 lines; a multi-repo walkthrough would push 25+. |
| 4 | Configuration surface moves to `docs/configuration.md`; CLI reference moves to `docs/cli.md`; README links to both | Per the brief: "Most readers should never need to scroll [to the configuration reference]." The reference content stays one click away, not one page-length of scrolling away. |
| 5 | No roadmap / CHANGELOG / PyPI / website work in this PR | Scope creep. Each of those is its own decision; this PR is the README refresh. |
| 6 | No rewrite of `## For AI agents` into the new README; replaced with one reference bullet pointing to `mship skill install` + working-with-mothership skill | The current section has ~35 lines of install instructions per platform, most of which is stale post-PR #55 (direct install to `~/.claude/skills/`) and post-PR #58 (the `--all` flag is gone). The one-bullet reference is the minimum faithful pointer; details belong in the skill docs, not the README. |
