# Fix a bug

**When you need this:** something's broken and you want the shortest safe path
to a merged fix.

## The fast path

Bugs and chores skip the design gates — no spec, no plan, just an isolated
worktree and a PR:

```bash
mship item new "500 on empty search" --kind bug
mship spawn "fix empty search 500" --work-item <wi-id>
cd $(mship status | jq -r '.resolved_task.worktrees | to_entries[0].value')

# reproduce, fix, commit
mship test
mship finish
```

That's the whole loop. The work item takes seconds and keeps the fix findable
later (what was broken, which task fixed it, which PR shipped it).

## Emergencies

If even `item new` is too much ceremony right now:

```bash
mship spawn "hotfix prod 500" --hotfix
```

`--hotfix` bypasses the work-item requirement for this task (and
`mship finish --hotfix` does the same at finish time). Bypasses aren't silent —
each one is recorded to `.mothership/bypass-log.jsonl`, so the escape hatch
leaves a trail.

## What stays enforced

The fast path skips *design* gates, not safety:

- **Worktree isolation** — the fix still happens on a feature branch in its own
  worktree, never directly on `main`.
- **The edit guard** — commits (and agent edits) to a repo's main checkout are
  still refused while a task is active.
- **Test visibility** — `mship finish` still surfaces failing tests in any
  affected repo before you ship.

If the "bug fix" starts growing a design (new behavior, new surface area),
promote it: create a spec and follow [Ship a feature](ship-a-feature.md).
