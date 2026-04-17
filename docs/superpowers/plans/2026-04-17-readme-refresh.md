# README refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-17-readme-refresh-design.md`

**Goal:** Rewrite `README.md` to the new structure (≤60 lines after cut-20%), extract CLI + configuration reference into `docs/cli.md` and `docs/configuration.md`, and fix stale content (skill install, missing `context`/`dispatch` commands, new `finish` flags).

**Architecture:** Three file touches: create `docs/cli.md` and `docs/configuration.md` as pure moves from the current README, then rewrite `README.md` to the new structure. One cut-20% pass at the end.

**Tech Stack:** Markdown. `mistune` for parse validation (already present as transient dev dep from PR #60; available via `uv run python -c "import mistune"`).

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `docs/cli.md` | Full CLI command reference: lifecycle, inspection, maintenance, long-running services, `mship finish` detail (base branch, body flags), drift audit & sync, live views, multi-task resolution rules | **create** |
| `docs/configuration.md` | `mothership.yaml` options, secret management (`env_runner`), monorepo (`git_root`), service start modes, healthchecks, task aliasing, Taskfile contract | **create** |
| `README.md` | New structure: title, one-sentence description, status, Problem, Quickstart, How it works, Common patterns, Scope, Reference, License | rewrite |

Every line of content that moves out of README into `docs/` must still exist somewhere; nothing is dropped. The rewrite is addition + relocation + deletion, not net content loss.

---

## Task 1: Create `docs/cli.md` (pure move + stale-content fix)

**Files:**
- Create: `docs/cli.md`
- No README edits yet.

- [ ] **Step 1.1: Create the file with the new CLI reference**

Create `docs/cli.md` with exactly this content (the structure mirrors the current README's CLI sections, with three additions: `mship context`, `mship dispatch`, and updated `finish` flag list):

````markdown
# CLI Reference

All task-scoped commands (`status`, `phase`, `test`, `journal`, `view …`, etc.) resolve their target task in this priority order:

1. `--task <slug>` flag — explicit, highest priority.
2. `MSHIP_TASK` env var — scope a whole shell session to one task.
3. cwd — if your shell is inside a task's worktree, that task is the default.

With 0 active tasks the command errors with "no active task". With exactly 1 active task and no anchor, the command targets that task. With 2+ active tasks and no anchor you'll get an "Ambiguous" error listing the active slugs — fix by anchoring via any of the three mechanisms above.

## Lifecycle

```bash
mship init [--detect | --name N --repo PATH:TYPE]   # scaffold mothership.yaml
mship init --install-hooks                          # (re)install pre-commit guard on every git root
mship spawn "description" [--repos a,b] [--skip-setup] [--bypass-reconcile]
mship switch <repo>                                 # cross-repo context switch
mship phase plan|dev|review|run [-f]                # transition with soft-gate warnings
mship block "reason" | mship unblock
mship test [--all] [--repos|--tag] [--no-diff]
mship journal [-]                                   # read task log; pass message to append
mship journal "msg" [--action X] [--open Y] [--repo R] [--test-state pass|fail|mixed]
mship journal --show-open                           # list open questions
mship finish [--body-file PATH | --body TEXT] [--base B] [--base-map a=B,b=B] [--push-only] [--handoff] [--force-audit] [--bypass-reconcile] [--force]
mship close [--yes] [--abandon] [--force] [--skip-pr-check] [--bypass-reconcile]
```

## Inspection

```bash
mship status                                        # task, phase, branch, drift, last log, finished warning
mship context                                       # one-shot agent-readable JSON snapshot of workspace state
mship dispatch --task <slug> -i "<instruction>"     # emit self-contained subagent prompt to stdout
mship audit [--repos r] [--json]
mship reconcile [--json] [--ignore SLUG] [--clear-ignores] [--refresh]
mship view status|logs|diff|spec [--watch]
mship view spec --web                               # serve rendered spec on localhost
mship graph
mship worktrees
mship doctor
```

## Maintenance

```bash
mship sync [--repos r]                              # fast-forward behind-only clean repos
mship prune [--force]                               # remove orphaned worktrees
```

## Long-running services

```bash
mship run [--repos a,b] [--tag t]                   # start services per dependency tier
mship logs <service>                                # tail logs for a service
```

## `mship finish`

### PR body

`mship finish` rejects empty PR bodies. Two ways to provide one:

```bash
mship finish --body-file /tmp/pr-body.md            # read from file
echo "..." | mship finish --body-file -             # read from stdin
mship finish --body "inline text"                   # inline (also supports `-` for stdin)
```

A TTY guard on both `-` forms errors fast if stdin is an interactive terminal instead of hanging.

### PR base branch

Each repo's PR can target a non-default base. Resolution order (most-specific wins):

- `--base <branch>` — global override for all repos.
- `--base-map cli=main,api=release/x` — per-repo overrides.
- `base_branch` in the repo's `mothership.yaml` entry.
- Remote default branch.

`mship finish` verifies every resolved base exists on `origin` before any push.

### `--force` vs normal re-finish

`mship finish` is idempotent: a second run after `finished_at` is stamped is a no-op. To push additional commits to the existing PRs (e.g., reviewer feedback), use `mship finish --force`. It pushes, updates `finished_at`, writes a `re-finished` journal entry, and does NOT create a new PR or modify the existing body. Edit the body separately via `gh pr edit <url> --body-file <path>`.

## Drift audit & sync

### Issue codes

- Errors (block `spawn`/`finish`): `path_missing`, `not_a_git_repo`, `fetch_failed`, `detached_head`, `unexpected_branch`, `dirty_worktree`, `no_upstream`, `behind_remote`, `diverged`, `extra_worktrees`.
- Warnings (don't block): `dirty_untracked` (untracked files only).
- Info-only: `ahead_remote`.

### Per-repo policy

```yaml
repos:
  schemas:
    path: ../schemas
    expected_branch: marshal-refactor
    allow_dirty: false
    allow_extra_worktrees: false
```

### Workspace policy

```yaml
audit:
  block_spawn: true
  block_finish: true
```

### Commands

- `mship audit [--repos r1,r2] [--json]` — exit 1 on any error-severity drift.
- `mship sync [--repos r1,r2]` — fast-forwards behind-only clean repos.
- `mship spawn --force-audit` / `mship finish --force-audit` — bypass with a line logged to the task log.

## Live views

`mship view` provides read-only TUIs designed for tmux/zellij panes. All views support `--watch` and `--interval N`.

- `mship view status [--task <slug>] [--watch]` — all tasks stacked by default; `--task` narrows to one.
- `mship view logs [--task <slug>] [--watch]` — tail the task's log.
- `mship view diff [--task <slug>] [--watch]` — per-worktree git diff.
- `mship view spec [name-or-path] [--task <slug>] [--watch] [--web]` — cross-task spec index picker by default.

Keys: `q` quit, `j/k` or arrows to scroll, `PgUp/PgDn`, `Home/End`, `r` force refresh.
````

- [ ] **Step 1.2: Markdown parse validation**

```bash
uv run python -c "
import mistune, pathlib
p = pathlib.Path('docs/cli.md')
html = mistune.html(p.read_text())
assert '<h1' in html and '<h2' in html and '<code>' in html, 'lost structure'
# Every command we added must be mentioned:
for cmd in ('mship context', 'mship dispatch', '--body-file', '--force'):
    assert cmd in p.read_text(), f'missing: {cmd}'
print(f'OK — {len(html)} chars rendered')
"
```

Expected: `OK — <N> chars rendered`.

- [ ] **Step 1.3: Commit (pair with `mship journal` in a mothership workspace)**

```bash
git add docs/cli.md
git commit -m "docs: add CLI reference at docs/cli.md (moved from README; adds context/dispatch/body-file)"
mship journal "extracted CLI reference to docs/cli.md; added context, dispatch, and finish body-file coverage" --action committed
```

---

## Task 2: Create `docs/configuration.md` (pure move)

**Files:**
- Create: `docs/configuration.md`
- No README edits yet.

- [ ] **Step 2.1: Create the file with the configuration reference**

Create `docs/configuration.md` with exactly this content:

````markdown
# Configuration

## `mothership.yaml`

```yaml
workspace: my-platform

# Optional: wraps all task execution with a secret manager
env_runner: "dotenvx run --"

# Optional: branch naming pattern ({slug} is replaced)
branch_pattern: "feat/{slug}"

repos:
  shared:
    path: ./shared
    type: library            # "library" or "service"
    depends_on: []
    env_runner: "op run --"  # per-repo override
    tasks:
      test: unit             # override canonical task name
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
```

## Secret management (`env_runner`)

Mothership doesn't manage secrets. It delegates to your secret manager via `env_runner`:

| Tool | Config value |
|------|-------------|
| dotenvx | `dotenvx run --` |
| Doppler | `doppler run --` |
| 1Password CLI | `op run --` |
| Infisical | `infisical run --` |
| None | omit `env_runner` |

## Monorepo support (`git_root`)

For monorepos where multiple services share one git repo, use `git_root` to declare subdirectory services:

```yaml
repos:
  backend:
    path: .
    type: service
  web:
    path: web              # relative — interpreted against backend's worktree
    type: service
    git_root: backend
    depends_on: [backend]
```

Rules:
- `git_root` must reference another repo in the workspace.
- The referenced repo cannot itself have `git_root` set (no chaining).
- The subdirectory must exist and contain a `Taskfile.yml`.
- Subdirectory services still have their own `depends_on`, `tags`, `tasks`, and `start_mode`.

## Service start modes (`start_mode`)

For long-running services, set `start_mode: background`:

```yaml
repos:
  infra:
    path: ./infra
    type: service
    start_mode: background     # mship run launches and moves on
  backend:
    path: ./backend
    type: service
    start_mode: background
    depends_on: [infra]
```

With `start_mode: background`, `mship run` launches the service and continues to the next dependency tier without waiting for exit. Background services keep running until Ctrl-C propagates SIGINT through go-task to their child processes. `start_mode` only affects `mship run`. Tests and logs always run foreground.

## Healthchecks

For services that need time to become ready, declare a `healthcheck`. `mship run` waits for the healthcheck to pass before starting dependent services.

```yaml
repos:
  infra:
    path: ./infra
    type: service
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:8001"          # wait for port to accept connections
      timeout: 30s                    # optional, default 30s
      retry_interval: 500ms           # optional, default 500ms

  backend:
    path: ./backend
    type: service
    start_mode: background
    depends_on: [infra]
    healthcheck:
      http: "http://localhost:8000/health"

  web:
    path: ./web
    type: service
    start_mode: background
    depends_on: [backend]
    healthcheck:
      sleep: 3s                        # unconditional wait

  custom:
    path: ./custom
    type: service
    start_mode: background
    healthcheck:
      task: wait-for-custom            # runs `task wait-for-custom`; 0 exit = ready
```

Probe types: `tcp`, `http`, `sleep`, `task` (any one per healthcheck). Exactly one probe per healthcheck. If the probe doesn't succeed within `timeout`, the service is treated as failed and `mship run` exits non-zero. Healthchecks apply to `mship run` only.

## Task name aliasing

If your Taskfile uses different task names than mothership's defaults (`test`, `run`, `lint`, `setup`), add a `tasks:` mapping:

```yaml
repos:
  my-app:
    path: .
    type: service
    tasks:
      run: dev                 # mship run → task dev
      test: test:all           # mship test → task test:all
      lint: lint:all
      setup: infra:start
```

`mship doctor` respects the mapping when checking for standard tasks.

## Taskfile contract

Each repo needs a `Taskfile.yml` with standard task names. Mothership calls `task <name>` in each repo. Override names per repo in the `tasks` mapping. Default tasks: `test`, `run`, `lint`, `logs`, `setup`. Missing tasks are skipped gracefully.
````

- [ ] **Step 2.2: Markdown parse validation**

```bash
uv run python -c "
import mistune, pathlib
p = pathlib.Path('docs/configuration.md')
html = mistune.html(p.read_text())
assert '<h1' in html and '<h2' in html and '<code>' in html, 'lost structure'
for key in ('mothership.yaml', 'env_runner', 'git_root', 'start_mode', 'healthcheck', 'tasks:'):
    assert key in p.read_text(), f'missing: {key}'
print(f'OK — {len(html)} chars rendered')
"
```

Expected: `OK — <N> chars rendered`.

- [ ] **Step 2.3: Commit**

```bash
git add docs/configuration.md
git commit -m "docs: add configuration reference at docs/configuration.md (moved from README)"
mship journal "extracted configuration reference to docs/configuration.md" --action committed
```

---

## Task 3: Rewrite `README.md` to the new structure

**Files:**
- Modify: `README.md` (near-total rewrite; keep MIT license line)

- [ ] **Step 3.1: Replace README.md with the new content**

Overwrite `README.md` entirely with this content:

````markdown
# mship

State safety for an AI agent working across your git repos: isolated worktrees per task, coordinated PRs, durable cross-session state.

> Pre-1.0. API may change. Pin a commit if you need stability.

## Problem

AI agents given write access across repos routinely commit to `main` because they forgot `git checkout`, modify files in the wrong worktree, lose track of what they changed in repo A while working in repo B, and merge coordinated PRs in the wrong order. These are state-management failures, not agent-skill failures — git alone doesn't model them.

## Quickstart

```bash
uv tool install git+https://github.com/atomikpanda/mothership.git

cd my-project
mship init --name my-project --repo .:service
mship spawn "add hello world"
cd $(mship status | jq -r '.worktrees."my-project"')

echo 'print("hello")' > hello.py
git add hello.py
git commit -m "feat: hello world"

cat > /tmp/body.md <<'EOF'
## Summary
First mship task.

## Test plan
- [x] Runs locally.
EOF
mship finish --body-file /tmp/body.md
```

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/). Optional: [go-task](https://taskfile.dev), [gh](https://cli.github.com) (for `mship finish`).

## How it works

Each task lives in a git worktree on its own feature branch, one per affected repo. mship manages the worktrees, tracks state in `.mothership/state.yaml`, and gates transitions (`spawn`, `finish`, `close`) on git-state audits. The agent reads structured state via `mship status`, `mship journal`, and `mship context`; it writes via mship commands that update state atomically. A pre-commit hook blocks commits from outside the active task's worktrees.

## Common patterns

**Multi-repo task.** `mship spawn "refactor schema" --repos api,client` creates one worktree per repo on the same feature branch. `mship test` runs them in dependency order, wiring each worktree in as the other's dependency. `mship finish` opens PRs in dependency order with cross-repo coordination blocks.

**Agent session handoff.** `mship dispatch --task <slug> -i "<instruction>"` emits a self-contained prompt — cd directive, branch state, recent journal entries, finish contract — for a fresh subagent. No parent-held context required.

## Scope

- **Does:** isolate writes via worktrees, sequence cross-repo work, gate transitions on git state, surface structured state to agents.
- **Does not:** run the agent, generate code, manage secrets (delegates to `env_runner`), replace your CI.
- **Works for:** a single repo, a monorepo (via `git_root`), or multiple repos in one workspace.

## Reference

- [`docs/cli.md`](docs/cli.md) — full command surface.
- [`docs/configuration.md`](docs/configuration.md) — `mothership.yaml` options, healthchecks, service start modes, monorepo rules, task aliasing.
- `mship skill install` — installs a bundle of agent-side skills (including `working-with-mothership` — session-start protocol, phase workflow, recovery patterns).

## License

MIT
````

- [ ] **Step 3.2: Markdown parse validation**

```bash
uv run python -c "
import mistune, pathlib
p = pathlib.Path('README.md')
html = mistune.html(p.read_text())
assert '<h1' in html and '<h2' in html and '<code>' in html, 'lost structure'
# Stale content must be gone:
for stale in ('/plugin marketplace', 'skill install --all', 'gh pr create'):
    assert stale not in p.read_text(), f'stale content leaked: {stale}'
# New one-sentence description must be present:
assert 'State safety for an AI agent working across your git repos' in p.read_text()
# Links to docs/ must resolve:
for link in ('docs/cli.md', 'docs/configuration.md'):
    assert link in p.read_text(), f'missing reference link: {link}'
    assert pathlib.Path(link).is_file(), f'link target missing: {link}'
print(f'OK — {len(html)} chars rendered; line count = {len(p.read_text().splitlines())}')
"
```

Expected: `OK — <N> chars rendered; line count = <M>` where M is in the 55–70 range.

- [ ] **Step 3.3: Commit**

```bash
git add README.md
git commit -m "docs(readme): rewrite to one-page shape — problem, quickstart, how, patterns, scope"
mship journal "rewrote README.md to spec: ~60 lines, doc references out, stale content removed" --action committed
```

---

## Task 4: Cut-20% pass

**Files:**
- Modify: `README.md` (trim; no content additions)

- [ ] **Step 4.1: Count lines and identify candidates**

```bash
wc -l README.md
```

If the line count is ≤50, skip to Step 4.3. If >50, apply the cut.

Candidates in priority order (per spec's cut list):
- Redundant `&&` chaining in the Quickstart (split into separate commands if that makes the intent clearer and adds no lines; leave alone otherwise).
- Adjectives that snuck back in ("simple," "just," "quickly," "robust," "powerful"): search with `grep -E "\\b(simple|just|quickly|robust|powerful|seamless|elegant)\\b" README.md`.
- Second-level sentences in `## How it works` that restate what the Quickstart already showed.
- In `## Common patterns`, any clause that enumerates features rather than answering "why reach for this."

- [ ] **Step 4.2: Apply the cut**

Edit `README.md` to remove the identified fat. Re-run:

```bash
wc -l README.md
grep -E "\\b(simple|just|quickly|robust|powerful|seamless|elegant)\\b" README.md
```

Expected: line count ≤ 55; the grep returns no matches.

- [ ] **Step 4.3: Re-run the parse validation from Step 3.2**

```bash
uv run python -c "
import mistune, pathlib
p = pathlib.Path('README.md')
html = mistune.html(p.read_text())
for stale in ('/plugin marketplace', 'skill install --all', 'gh pr create'):
    assert stale not in p.read_text(), f'stale content leaked: {stale}'
assert 'State safety for an AI agent working across your git repos' in p.read_text()
for link in ('docs/cli.md', 'docs/configuration.md'):
    assert link in p.read_text(), f'missing reference link: {link}'
print(f'OK — {len(html)} chars; {len(p.read_text().splitlines())} lines')
"
```

Expected: still passes; line count reflects the cut.

- [ ] **Step 4.4: Commit (if any lines changed)**

If the cut removed anything:

```bash
git add README.md
git commit -m "docs(readme): cut-20% pass — trim adjectives and restatements"
mship journal "applied cut-20% pass; README now <N> lines" --action committed
```

If the first draft was already ≤50 lines, skip this commit.

---

## Task 5: Copy-paste verification of the Quickstart

**Goal:** validate the Quickstart end-to-end. No files change. This catches placeholder leaks, missing prerequisites, and commands that only work in the author's environment.

- [ ] **Step 5.1: Run the Quickstart in a scratch directory**

```bash
# In a brand-new temp workspace, NOT inside the mship repo:
cd /tmp && rm -rf readme-smoke && mkdir readme-smoke && cd readme-smoke
git init -b main -q
git config user.email t@t && git config user.name t
cat > Taskfile.yml <<'EOF'
version: '3'
tasks: {}
EOF
git add Taskfile.yml && git commit -qm init

# Now the Quickstart (minus `uv tool install`, which we already have):
uv tool run --from /home/bailey/development/repos/mothership/.worktrees/feat/readme-refresh-value-prop-5-min-walkthrough-install-cleanup mship init --name smoke --repo .:service
uv tool run --from /home/bailey/development/repos/mothership/.worktrees/feat/readme-refresh-value-prop-5-min-walkthrough-install-cleanup mship spawn "add hello world"
cd "$(uv tool run --from /home/bailey/development/repos/mothership/.worktrees/feat/readme-refresh-value-prop-5-min-walkthrough-install-cleanup mship status | jq -r '.worktrees."smoke"')"
echo 'print("hello")' > hello.py
git add hello.py && git commit -m "feat: hello world"
```

Don't run `mship finish` — that would try to push and open a GitHub PR against an origin that doesn't exist. The check stops one step short of finish. What this validates:
- `uv tool install` is the right install incantation (skipped — assumed present).
- `mship init --name X --repo .:service` produces a valid `mothership.yaml`.
- `mship spawn` creates a worktree.
- `mship status | jq -r '.worktrees."<name>"'` resolves to a real path (catches placeholder / quoting bugs).
- Editing inside the worktree + committing works (catches pre-commit hook surprises).

If any step fails, the Quickstart is wrong. Fix it in `README.md` and re-run Step 5.1.

- [ ] **Step 5.2: Cleanup**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/readme-refresh-value-prop-5-min-walkthrough-install-cleanup
rm -rf /tmp/readme-smoke
```

No commit for this task.

---

## Task 6: Final verification + PR

- [ ] **Step 6.1: 90-second test**

Open `README.md` in a fresh terminal or browser view. Without scrolling past the Scope section, answer:

1. What is it? — should be answerable from the one-sentence description + Problem.
2. Who is it for? — should be answerable from the one-sentence description.
3. How would I try it? — should be answerable from the Quickstart.
4. What does it not do? — should be answerable from Scope.

If any requires inference or scrolling further, go back to Task 4 for another trim, or Task 3 if the structure itself is wrong.

- [ ] **Step 6.2: Full suite (sanity)**

```bash
uv run pytest -x -q 2>&1 | tail -3
```

Expected: all pass. No code changed; this should be a pure no-op.

- [ ] **Step 6.3: Open the PR**

```bash
cat > /tmp/readme-body.md <<'EOF'
## Summary

Refreshed `README.md` per a written brief: optimize for a developer who might actually use the tool, arrive at "what is it / who is it for / how would I try it / what does it not do" in under 90 seconds, and signal engineering taste through restraint.

- Replaced the 461-line README with ~55 lines: title, one-sentence description, status, Problem, Quickstart, How it works, Common patterns, Scope, Reference, License.
- Extracted the full CLI reference to `docs/cli.md` (added `mship context`, `mship dispatch`, `finish --body-file`, `finish --force`; dropped the stale slash-command skill-install path).
- Extracted the configuration reference (mothership.yaml, env_runner, git_root, start_mode, healthchecks, task aliasing, Taskfile contract) to `docs/configuration.md`.
- Dropped marketing voice, feature-list-before-problem framing, and decorative badges. Removed "doesn't work for" edge case per the brief's "when in doubt, cut."

Prose rules enforced: active voice, present tense, no adjectives the Quickstart can't demonstrate, no emoji in headers, every code block has a language hint, every reference link resolves to a file in the repo.

## Test plan

- [x] `mistune.html()` parse check for README.md, docs/cli.md, docs/configuration.md.
- [x] Stale-content scan: `/plugin marketplace`, `skill install --all`, `gh pr create` no longer appear in README.
- [x] Link check: every `[...](docs/...)` link in the README resolves to an existing file.
- [x] Copy-paste smoke: Quickstart runs in a scratch directory up to (but not including) `mship finish`.
- [x] 90-second test: the four core questions answerable without scrolling past Scope.
- [x] Full pytest green (no code changed — pure sanity).
EOF
mship finish --body-file /tmp/readme-body.md
rm /tmp/readme-body.md
```
