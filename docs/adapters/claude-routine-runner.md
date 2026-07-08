# Reference Adapter: a Claude Routine as the Unattended-Run Host

This documents **one concrete host** for the unattended runner (spec
`unattended-runner`, AC8): a Claude routine on a cron schedule (or the
`cron+claude -p` pattern, or a scheduled cloud agent) that drives one tick of
`mship`'s pull API and hands the resulting prompt to an agent turn. The design
is host-agnostic — mship never spawns an agent and doesn't know or care what
runtime calls it — so everything here generalizes to any scheduler that can
run shell commands and invoke an LLM agent. This file is the reference
implementation for that contract, plus a smoke-test checklist for verifying a
deployment actually claims/emits/exits the way this doc says it will.

All commands below were verified against this branch's CLI (`mship --help`,
`mship item run-next --help`, `mship item bail --help`, etc.) rather than
assumed — see the smoke test section for the transcript.

## The contract, in one paragraph

Split control plane from execution plane. `mship` is the host-agnostic
control plane: it selects the next eligible WorkItem, claims it (git-backed,
so ephemeral/serverless runs coordinate through nothing but a git remote),
and emits a self-contained dispatch prompt. The agent runtime — Claude
routine, cron, CI, whatever — is the execution plane: it runs that prompt,
calling `mship` commands as it works, and opens a PR. **It never merges.**
On an unresolvable fork or unfixable failure it calls `mship item bail`,
which records the reason, marks the item blocked, and releases the claim so
a later tick (or an attended human) can pick it up. One item per tick (v1 is
one-at-a-time; no parallel runs).

## Prerequisites

- `mship` installed and on `PATH` in the routine's execution environment.
- A git remote the routine's environment can both fetch from and push to —
  the run-state ref (see below) is committed and pushed as checkpoints, so a
  read-only clone is not enough.
- Two environment variables, set on the routine (not mship flags — these are
  adapter-level inputs the routine's shell body needs before it can call
  `mship` at all):
  - **`GH_TOKEN`** (or `GITHUB_TOKEN`, checked in that order) — a GitHub
    token with repo scope. `mship bootstrap` uses it to clone private member
    repos and `mship finish` uses it to push branches + open PRs, in either
    case only when no other git credential helper is already configured.
    Both commands also accept an explicit `--token`, which wins over either
    env var (`resolve_token`'s precedence: `--token` > `GH_TOKEN` >
    `GITHUB_TOKEN`, `src/mship/core/gh_auth.py`).
  - **`WORKSPACE_GIT_URL`** — the git URL of the *workspace meta-repo* (the
    one containing `mothership.yaml`). This is an adapter convention, not an
    mship flag: `mship bootstrap` has no positional "clone this workspace"
    argument — it only clones the **member** repos a workspace already
    declares (from each repo's `url:` or the workspace's `default_remote:`
    in `mothership.yaml`). Getting `mothership.yaml` onto disk in the first
    place is a plain `git clone`, which is the routine's job, not mship's.

## Eligibility: what makes an item pick-able

`mship item run-next` selects the oldest-first candidate where **all** of:

- `unattended` is `true` on the WorkItem (opt-in; set via `mship item
  unattended <id> --on` or the Ground Control checkbox on the WorkItem —
  both flip the same flag),
- its phase is `ready` (v1: `phase_override == "ready"`),
- it has a linked spec whose status is `approved`,
- it is not currently claimed on the run-state ref.

If nothing matches, the tick is a clean no-op.

## The routine, one tick

```bash
set -euo pipefail

# --- environment the routine must provide ---
#   GH_TOKEN            GitHub token (repo scope); or GITHUB_TOKEN.
#   WORKSPACE_GIT_URL   git URL of the repo containing mothership.yaml.

WORKDIR="${WORKDIR:-/work/workspace}"

# 1. Materialize the workspace meta-repo. Ephemeral cloud runs assume no
#    persistent disk between ticks, so this is a fresh `git clone` every
#    time; on a persistent host it's a no-op `git pull` instead. Either way,
#    end up at a directory containing mothership.yaml.
if [ ! -f "$WORKDIR/mothership.yaml" ]; then
  git clone "$WORKSPACE_GIT_URL" "$WORKDIR"
fi
cd "$WORKDIR"

# 2. Materialize the workspace's member repos. Idempotent: an already-
#    present member reports "present" and is left untouched (no-clobber),
#    so re-running this every tick on a persistent host is safe too.
mship bootstrap

# 3. Pull: claim + emit the next eligible item's dispatch prompt.
#    Non-TTY output is JSON: {"runnable": true, "item_id", "prompt"}
#    or {"runnable": false}.
result="$(mship item run-next)"
runnable="$(printf '%s' "$result" | jq -r '.runnable')"

if [ "$runnable" != "true" ]; then
  echo "nothing runnable this tick"
  exit 0
fi

item_id="$(printf '%s' "$result" | jq -r '.item_id')"
prompt="$(printf '%s' "$result" | jq -r '.prompt')"

# 4. Hand the prompt to a fresh agent turn. This is the execution-plane seam
#    — swap this one line for any agent runtime. For the cron+`claude -p`
#    pattern:
#
#      claude -p "$prompt" --permission-mode acceptEdits
#
# The agent turn (see "What the agent does" below) either finishes cleanly
# (tests green, PR opened) or calls `mship item bail "$item_id" --reason
# "..."` on a fork/failure. Either way this script's job ends when that
# turn returns — one item per tick, no retry loop, no second run-next call.
```

### What the agent does with the prompt

The emitted `prompt` is spec-first: it renders the WorkItem id, the linked
spec's `## Problem` section, and its acceptance criteria, then instructs the
agent to "Implement this work item to satisfy its approved spec, then finish
per workspace conventions." If the item already has commits from a prior
(bailed or interrupted) run, `mship item run-next` prepends a `## RESUMING
prior run` preamble naming the branch, commits-ahead count, and the last few
journal lines, and tells the agent not to restart.

Concretely, the agent:

1. If the item has no linked task yet (a fresh pickup), spawns one and links
   it in the same step: `mship spawn "<title>" --work-item <item_id> --yes`
   (`--yes` matters — the routine's shell is never a TTY, and spawn requires
   it to skip confirmation prompts above `spawn_confirm_threshold`). This
   also links the new task to the WorkItem automatically, which is what lets
   a *later* tick's resumable-dispatch wrapper find the branch and journal.
2. Works the task using the normal mship loop: `mship context` for a
   workspace snapshot, edits, `mship test` until green, `mship journal` to
   record progress, `mship ask` if it needs a non-blocking decision surfaced
   to the phone.
3. On success: `mship finish` — creates the PR. `mship finish` still
   enforces its existing gates (approved spec, audit, passing tests); it is
   never bypassed by the unattended path.
4. **Never merges the PR and never pushes to the base branch directly.** A
   human reviews and merges in the morning.
5. On an unresolvable fork (a design decision it can't make unattended) or a
   test failure it can't fix: `mship item bail <item_id> --reason "<why>"`,
   then stop. It does not retry, does not pick a different item, and does
   not merge anything.

## Rules

**Never merge.** Unattended execution stops at an open PR every time — spec
non-goal, not a bug. `mship finish` opens PRs; nothing in this loop calls
`git merge`, `gh pr merge`, or pushes to the base branch.

**Bail, don't block.** `mship item bail <id> --reason "<reason>"`
(`checkpoint_bail`, `src/mship/core/runner.py`) does three things in this
order: logs the reason to the item's run-log (durable even if the next two
steps race), marks the item's linked task(s) `blocked_reason` (so the
selector's *human-facing* "blocked" flag lights up), then releases the
claim. The branch is left intact — a bail is a checkpoint, not a rollback;
a later tick (or an attended human) can resume off exactly where it stopped.

**One item per tick.** The routine calls `mship item run-next` exactly once
per invocation. It does not loop internally to drain the backlog — draining
N eligible items takes N scheduled ticks. This is deliberate for v1 (no
parallel runs, see the spec's non-goals); a farm/concurrency model is future
work.

## Known limitation: `bail`'s claim release is cross-process best-effort

The run-state claim's holder token is `hostname:pid`
(`_run_holder()`, `src/mship/cli/workitem.py`), minted fresh by *each* CLI
invocation. `mship item run-next` and `mship item bail` are always separate
`mship` process invocations in real use — there is no way to invoke both
from the same OS process outside of a test harness. That means the `pid` in
`bail`'s holder token never matches the `pid` that made the original claim,
so `RunStateRepo.release()`'s holder-identity check (`existing.holder ==
holder`) does not match and the release is a silent no-op — verified during
the smoke test below (the claim file was still present after `bail`
completed). The reason is still logged and the item is still marked blocked,
so the operator sees a bailed item — but the run-state claim itself only
clears once its TTL (`RunStateRepo`'s default 1800s / 30 minutes) elapses.
Practically: an item bailed by this adapter won't be pick-able by
`run-next` again for up to 30 minutes, even though it isn't actually held by
a live run. This doesn't threaten correctness (a stale claim just delays a
retry it currently wouldn't need to survive), but it's worth knowing before
assuming an immediate re-tick will pick a just-bailed item back up.

## Smoke test checklist

Run this in a throwaway workspace (not a real one — it creates a scratch
item + spec) before trusting a new deployment of this adapter. It exercises
exactly the two outcomes `mship item run-next` can produce.

Setup: a workspace with a git `origin` remote configured (the run-state ref
needs somewhere to push to — even a local bare repo works for this check).

1. **No eligible item → `{"runnable": false}`.**
   In a workspace with nothing unattended/ready/approved yet:
   ```bash
   mship --json item run-next
   # {"runnable": false}
   ```
   Exit code 0 either way — this is a normal empty tick, not an error.

2. **One approved + unattended item → prints a prompt and records a claim.**
   ```bash
   ITEM_ID=$(mship item new "Smoke test item" --kind feature)
   mship spec new --title "Smoke test item" --id smoke-1
   printf '%s' '{"problem":"p","user_story":"u","approach":"a"}' \
     | mship spec apply smoke-1 --from-json -
   mship spec approve smoke-1 --bypass-gate   # or a real review pass
   mship item link-spec "$ITEM_ID" smoke-1
   mship item phase "$ITEM_ID" ready
   mship item unattended "$ITEM_ID" --on

   mship --json item run-next
   # {"runnable": true, "item_id": "<ITEM_ID>", "prompt": "# Unattended run: ..."}
   ```
   Confirm the `prompt` field mentions the item id and the spec's Problem
   text.

3. **The claim actually holds** (proves step 2 didn't just print a prompt
   but also recorded a claim other runs will respect): immediately invoke
   `run-next` again, as a *separate* process (a new shell, not a loop in the
   same script) — a real second tick would be exactly this.
   ```bash
   mship --json item run-next
   # {"runnable": false}
   ```
   With only one eligible item in the backlog, a second concurrent/immediate
   pull must come back empty — the first claim is still live. If this
   instead returns the same item again, the claim isn't being honored and
   something regressed.

4. **Bail releases (eventually) and records the reason.**
   ```bash
   mship item bail "$ITEM_ID" --reason "smoke test"
   # {"item_id": "<ITEM_ID>", "bailed": true, "reason": "smoke test"}
   ```
   Confirm the item shows as blocked (if it has a linked task,
   `blocked_reason` is set on that task) and the reason string appears
   somewhere retrievable for the operator. Per the known limitation above,
   don't expect `run-next` to immediately re-offer this item — that's
   correct, not a bug, until the claim TTL elapses.

This exact sequence (minus the throwaway IDs) was run against this branch's
CLI while writing this doc and produced the outputs shown above.
