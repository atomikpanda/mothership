# Systematic Debugging Loop — Design

Closes #30.

## Problem

When something breaks mid-task, there's no sanctioned mship-aware debugging flow. The `systematic-debugging` skill has the methodology; the journal has durable per-task narrative; the two don't connect. Agents debug in free-form, logging nothing structured, and next session (or next agent) has to re-derive the search tree from commit messages.

Frontier research (SWE-agent, Debug-gym, AgentRx, CodeTracer, HypoExplore) converges on a pattern: **durable trace with explicit hypothesis vocabulary, tight coupling between methodology skill and storage tool, evidence-linked entries that tree-compilers can consume.**

## Solution

Three-part fix:

1. **Thin `mship debug` sub-app** adds convenience wrappers over the existing journal for a named action vocabulary: `debug-start`, `hypothesis`, `ruled-out`, `debug-resolved`.
2. **`mship test` integration**: when an open debug thread exists, the existing "ran tests" journal entry gains a `parent=<hypothesis-id>` field. One journal entry per test run, debug context inlined — exactly what tree-compilation tools (CodeTracer, AgentStepper) consume happily.
3. **`systematic-debugging` skill update**: the methodology MANDATES mship invocation when mship is present (tight coupling). Without mship, falls back to prior free-form methodology (graceful degradation).

## Research alignment (2026)

Summary of validated design decisions from two rounds of literature review via Perplexity:

| Decision | Research signal | Our design |
|---|---|---|
| Explicit thread closure | SWE-agent: explicit submit universal norm; implicit closure = "greedy agent anti-pattern." AgentRx: phase boundaries must be explicit. | `debug-resolved` is SOLE closure signal. No implicit close. |
| IDs | NVIDIA NeMo: UUID format standard for correlation. | Auto-generated 8-char UUID prefix per entry. `--id <slug>` for human-readable override. |
| Evidence refs | Agent Trace v0.1.0: `<path>:<start>-<end>` + optional content hash. | Free-form string via `--evidence`. Skill doc cites the convention; no enforcement. |
| Action vocabulary | HypoExplore: confirmed/uncertain/refuted triad. AgentRx: 10-category failure taxonomy. | Keep coarse `hypothesis` / `ruled-out`. Optional `--category <label>` flag for AgentRx-style tagging; not prescribed. |
| Subagent isolation | Workspace DNA: isolated subagent execution; shared representation layer via controller. | Already matches — subagent-driven-development pattern has isolated workers; controller holds journal. No code change. |
| Test integration | CodeTracer: verification outcome bundled into the triggering action's step record. | `mship test` enriches its existing "ran tests" entry with `parent=<hypothesis-id>` when debug thread open. ONE entry, not two. |
| Skill-tool coupling | Research: tight coupling reduces hallucination-driven shortcuts for debugging specifically. | Skill MANDATES mship invocation when present; falls back without. |

## Scope

### In scope

- New `src/mship/cli/debug.py` — typer sub-app `mship debug` with three verbs: `hypothesis`, `rule-out`, `resolved`.
  - `mship debug start` is NOT needed: the first `mship debug hypothesis` implicitly opens a thread. `start` would be a ceremony command with no additional data beyond the first hypothesis.
  - Rationale: simplifying a command away when its value is zero.
- Auto-generated 8-char UUID prefix per entry, stored as `id=<prefix>` journal kv. User can override with `--id <slug>`.
- Evidence refs via `--evidence <ref>` — free-form string stored as `evidence=<ref>` kv.
- Parent pointers via `--parent <id-or-slug>` — stored as `parent=<ref>` kv. User-supplied; no code enforces DAG validity.
- Optional `--category <label>` on `ruled-out` — stored as `category=<label>` kv.
- `mship test` integration: when `current_debug_thread(log, slug)` is non-empty, enrich the existing test-run journal entry with `parent=<latest-unresolved-hypothesis-id>`.
- `current_debug_thread` helper in `src/mship/core/debug.py` — pure function over journal entries.
- Advisory stderr WARNING (not a block) when `debug-resolved` is logged without any prior `hypothesis` in the journal. Journal write still succeeds.
- Skill doc update in `src/mship/skills/systematic-debugging/SKILL.md` (create if absent) or `src/mship/skills/working-with-mothership/SKILL.md` — whichever owns the debugging methodology. Tight-coupling text mandating the mship commands when present.

### Out of scope (v2+)

- HypoExplore `confirmed` / `uncertain` / `refuted` hypothesis state mutations.
- AgentRx 10-category taxonomy as a prescribed enum (free-form `--category` is enough).
- Content-hash evidence refs.
- Formal delegation checkpoints in the journal.
- Cross-task debug memory / resolution library.
- Loop interruption / semantic-similarity detection on repeated hypotheses.
- `mship debug show` render command.
- `mship status` integration showing open-thread count.
- Enforcement of parent-id DAG validity.

## Architecture

One module, one helper, one CLI file, one doc update.

```
mship.core.debug
  └─ current_debug_thread(log_mgr, slug) -> list[LogEntry] | None
       (latest debug-start-implied-by-first-hypothesis through optional
        debug-resolved; None if no open thread)

mship.cli.debug.register(app, get_container)
  ├─ mship debug hypothesis "<text>" [--evidence] [--id] [--task]
  ├─ mship debug rule-out  "<text>" [--evidence] [--parent] [--category] [--task]
  └─ mship debug resolved  "<text>" [--task]

mship.cli.exec.test (EXISTING)
  └─ new: after existing journal.append("ran tests", ...),
          check current_debug_thread; if open, rewrite the last entry
          OR append additional parent=<id> kv
       (depends on LogManager API — design note below)

mship.skills.<path>/SKILL.md
  └─ methodology MUST invoke mship debug <verb> when mship is present
```

**LogEntry kv extension**: the existing journal format already carries free-form kv after the timestamp header (`repo=`, `iter=`, `test=`, `action=`, `open=`). Adding `id=`, `parent=`, `evidence=`, `category=` is purely additive — `LogEntry` dataclass gains optional fields; `_format_kv` and `_parse_kv` in `src/mship/core/log.py` already emit and re-parse arbitrary kv pairs. **No format version bump.**

**Test integration implementation note**: `mship test` today calls `log_mgr.append(slug, msg, iteration=N, test_state=..., action="ran tests")`. We need to add `parent=<id>` when debug thread is open. Clean path:

1. Compute `parent_id = current_debug_thread(...)`-derived latest hypothesis id (or `None`).
2. `log_mgr.append(slug, msg, iteration=N, test_state=..., action="ran tests", parent=parent_id)` — extend `LogManager.append` to accept `parent: str | None = None`.

No retroactive editing of journal entries; the kv is included at write time.

## Command contracts

### `mship debug hypothesis "<text>"`

```
Options:
  --evidence <ref>       Free-form evidence pointer (e.g. test-runs/5, HEAD, path:12-18)
  --id <slug>            User-readable identifier (default: auto 8-char UUID prefix)
  --task <slug>          Target task (standard task-scope flag)
```

Journal entry:
```
## 2026-04-23T14:03:12Z  action=hypothesis  id=a3f4c2e1  evidence="test-runs/5"
Flaky assertion may be timezone-dependent.
```

Resolves task via `resolve_for_command("debug", ...)`. No guardrails — writes the entry regardless of prior journal state. The "open thread" concept is derived at read time by `current_debug_thread`, not enforced at write time.

### `mship debug rule-out "<text>"`

```
Options:
  --evidence <ref>
  --parent <id-or-slug>  Points at the hypothesis being refuted
  --category <label>     Optional classification (e.g. "Intent-Plan Misalignment")
  --id <slug>
  --task <slug>
```

Journal entry:
```
## 2026-04-23T14:15:44Z  action=ruled-out  id=b7d9e2a0  parent=a3f4c2e1  evidence="test-runs/6" category="tool-output-misread"
Not timezone — test fixture sets TZ explicitly and the failure still fires.
```

### `mship debug resolved "<text>"`

```
Options:
  --task <slug>
```

Journal entry:
```
## 2026-04-23T14:32:01Z  action=debug-resolved  id=c8e1f3b4
Root cause: race on shared cache eviction. Fix in commit 7f3a1b2.
```

**Advisory guard**: if journal has NO prior `hypothesis` entry since task creation, emit a stderr WARNING (`WARNING: logging debug-resolved without any prior hypothesis entries`), then write the entry. This catches the "agent logs resolution without doing the methodology" case (SWE-agent "greedy tendency") without blocking.

### `mship test` during open thread

Unchanged CLI; changed journal output only. Today the test entry is:
```
## 2026-04-23T14:20:03Z  iter=5  test=pass  action=ran tests
iter 5: 3/3 passing
```

During an open debug thread it becomes:
```
## 2026-04-23T14:20:03Z  iter=5  test=pass  action=ran tests  parent=a3f4c2e1
iter 5: 3/3 passing
```

One additional kv. Tree compilers (CodeTracer / AgentStepper) consume the `parent` to fold test runs into their parent hypothesis.

## Thread detection

```python
def current_debug_thread(log: LogManager, slug: str) -> list[LogEntry] | None:
    """Return entries constituting the current open debug thread, or None.

    An "open thread" is the sequence starting from the FIRST `hypothesis` entry
    after the most recent `debug-resolved` entry (or from task start if no
    `debug-resolved` has ever been logged), continuing to the end of the journal.

    Returns None when there are no `hypothesis` entries or the last
    `debug-resolved` is newer than the last `hypothesis`.
    """
```

Pure function, journal-only, no state mutation. Tests cover: no hypotheses, one hypothesis, hypothesis-then-resolved-then-nothing, hypothesis-then-resolved-then-new-hypothesis, multiple interleaved threads.

## Skill doc tight coupling

Update `src/mship/skills/systematic-debugging/SKILL.md` (if it exists at that path) or the appropriate skill SKILL.md file. The methodology section gains:

```markdown
## mship integration (REQUIRED when mship is present)

If `mship` is available in PATH and the current working directory is inside an mship workspace, you MUST invoke the tool at each methodology checkpoint. This generates the durable audit trace the supervisor relies on.

- When forming a hypothesis: `mship debug hypothesis "<text>" --evidence <ref>`
- When refuting one: `mship debug rule-out "<text>" --parent <id> --evidence <ref>`
- When closing the investigation: `mship debug resolved "<root cause and fix>"`

If mship is NOT available (non-mship project, tool absent), apply the methodology as described elsewhere in this skill (inline notes, commit messages, etc.).
```

The "REQUIRED when present" framing keeps the skill usable in non-mship contexts without fragmenting the methodology.

## Testing

### Unit — `tests/core/test_debug.py` (new)

- `current_debug_thread` with empty journal → None.
- Single hypothesis → returns list of one entry.
- Hypothesis + rule-out without resolved → both entries (thread still open).
- Hypothesis + rule-out + resolved → None (closed).
- resolved without prior hypothesis → None (no thread to return).
- Resolved → new hypothesis → returns the new hypothesis onward.
- Multiple resolved/new-hypothesis cycles → returns the latest open segment.

### Integration — `tests/cli/test_debug.py` (new)

- `mship debug hypothesis "X"` writes journal entry with `action=hypothesis`, auto-generated `id`.
- `mship debug hypothesis "X" --id h1` uses the provided id.
- `mship debug hypothesis "X" --evidence test-runs/5` writes evidence kv.
- `mship debug rule-out "Y" --parent h1` writes parent kv.
- `mship debug rule-out "Y" --category "tool-misread"` writes category kv.
- `mship debug resolved "Z"` writes resolution entry.
- `mship debug resolved "Z"` without prior hypothesis → stderr WARNING emitted, journal entry still written, exit 0.

### Integration — `tests/cli/test_exec.py` (existing; add new test)

- `mship test` during open debug thread → journal entry for the test run contains `parent=<hypothesis-id>`.
- `mship test` with no open debug thread → journal entry has no parent kv (regression check).

## Output format

All commands:
- TTY: one-line confirmation (`→ task: <slug> (resolved via cwd)` breadcrumb + `journal: <action> id=<id>`).
- Non-TTY: JSON `{task, action, id, parent?, evidence?, category?, resolved_task, resolution_source}`.

## Anti-goals

- No state-file mutation (no `debug_started_at` / `debug_resolved_at` on Task).
- No hard blocks (every journal write succeeds; only advisory warnings).
- No `debug show` / `debug status` command in v1.
- No loop-detection, escalation, or similarity analysis — those are supervisor-layer concerns.
- No tree-reconstruction in mship — downstream tools compile `id` / `parent` kvs into trees.
- No cross-task debug memory.
