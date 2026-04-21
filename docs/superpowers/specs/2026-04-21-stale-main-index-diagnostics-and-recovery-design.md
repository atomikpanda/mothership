# Stale-main-index diagnostics + `mship sync` auto-recovery — Design

## Context

After a PR is merged on GitHub, the normal post-merge flow from the main checkout is:

```
$ git checkout main
$ mship sync      # fast-forward main, catch up
$ mship close --task <slug>
```

In at least two observed cases this session, `mship sync` refused with:

```
mothership: skipped (dirty_worktree — N modified tracked files)
```

Inspection showed the modified tracked files on main are **exactly the PR's changes** — the same diff that would come in from `origin/main` once main fast-forwards. The user didn't stage anything; the state appeared during the normal `mship finish` → GitHub-merge → return-to-main flow.

Recovery today requires: `git reset HEAD <files>`, `git checkout -- <files>`, `mship sync`. Manual, fragile, and the user loses the tiny confidence boost `mship sync` was supposed to provide.

Root cause is unknown. Hypotheses include:
- Git worktree operations writing to main's index via the shared `.git/` directory.
- A hook firing from a worktree but applying to main's path.
- `uv tool install --reinstall --from .` touching main during the session.
- Something in `mship finish`'s pipeline (gh CLI calls, body edits) producing a side effect on the main checkout.

Diagnosing requires data we don't have today: state snapshots captured at the moment of the anomaly. Shipping instrumentation now is what makes a root-cause fix possible later.

## Goal

Two-part change:

1. **Instrument** — add a lightweight diagnostics library that captures forensics snapshots (JSON) when commands detect anomalous state. No snapshots on the happy path; only on the specific conditions we care about.
2. **Mitigate** — teach `mship sync` to auto-recover when it sees the signature pattern (dirty main whose content matches the fast-forward delta). Users stop hitting the manual-recovery wall. The mitigation logs every recovery attempt via the diagnostics library so we still collect evidence for the future root-cause fix.

## Success criterion

**Before change:** user runs `git checkout main; mship sync` after their PR merges. Gets `dirty_worktree` refusal. Must manually reset and retry.

**After change:** user runs `git checkout main; mship sync`. Either:
- (If the bug recurs) `mship sync` auto-recovers, logs the event to `.mothership/diagnostics/<ts>-sync-dirty-main-pre-recovery.json`, fast-forwards, prints `"recovered from stale main state; diagnostic at <path>"`, continues.
- (If real user work is dirty) refuses with the existing error (unchanged behavior). Diagnostic is still captured.

`mship doctor` gains a row: `warn   diagnostics   N snapshots present — review or prune` when `.mothership/diagnostics/` is non-empty.

`mship finish` / `mship close` capture a snapshot if they detect main dirty immediately after exit (no user-facing warning; data collection only).

## Anti-goals

- **No root-cause fix yet.** The purpose is instrumentation; the root cause may need multiple reproduction cycles to nail down.
- **No new CLI subcommand** (`mship diagnostics`). Files live under `.mothership/diagnostics/`; user inspects with `cat`; doctor surfaces the count.
- **No automatic rotation** of diagnostic files. Doctor reports the count, user decides when to `rm -rf .mothership/diagnostics/`.
- **No change to the audit gate matrix.** Recovery happens BEFORE the gate's refusal becomes user-visible, as a preflight in `_result_for`.
- **No change to `mship finish` or `mship close` user-facing output.** Post-op sanity check writes the diagnostic silently.
- **No destructive auto-recover.** `git reset --hard` is NEVER used for recovery. `git stash --include-untracked` + `git stash pop` handles the recovery semantically so any real user work is always recoverable.
- **No new CLI flag** to opt into / out of recovery. The recovery is safe by construction (stash-based); it only applies when the pattern matches.

## Architecture

### 1. Diagnostics library — `src/mship/core/diagnostics.py` (new)

Single public function:

```python
def capture_snapshot(
    command: str,
    reason: str,
    state_dir: Path,
    *,
    repos: dict[str, Path] | None = None,
    extra: dict | None = None,
) -> Path | None:
    """Write a JSON forensics blob to <state_dir>/diagnostics/<ts>-<command>-<reason>.json.

    Returns the written path, or None on write failure (never raises).
    """
```

Captured keys:
- `captured_at` — ISO-8601 UTC timestamp.
- `command` — the invoking mship command name (`"sync"`, `"finish"`, `"close"`).
- `reason` — short tag (`"dirty-main-pre-recovery"`, `"dirty-main-post-op"`, etc.).
- `cwd` — where the command was invoked from.
- `mship_version` — from `importlib.metadata.version("mothership")`.
- `python_version` — `sys.version`.
- `path_env` — `os.environ.get("PATH")`.
- `repos` — per-repo dict of `{git_status_porcelain, head_sha, head_branch, upstream_tracking, reflog_tail (last 10), stash_count}`. Only populated when the caller passes `repos={name: path}`.
- `extra` — caller-provided free-form data (e.g., the stash output after a recovery attempt).

Write failure handling: `except OSError: logger.debug(...); return None`. Diagnostics is best-effort — never blocks the caller.

Filename format: `<iso8601-with-z>-<command>-<reason>.json`, with colons replaced by `-` for filesystem safety (e.g., `2026-04-21T14-23-08Z-sync-dirty-main-pre-recovery.json`).

Directory created with `mkdir(parents=True, exist_ok=True)` on first call.

### 2. `mship sync` recovery — edit `src/mship/core/repo_sync.py::_result_for`

Today `_result_for(repo_audit, cfg, shell)` early-returns `SyncResult(..., "skipped", "dirty_worktree — ...")` when a blocking code appears on the repo audit. The new behavior: when the ONLY blocking code is `dirty_worktree` (not combined with other blocking codes), run a recovery attempt. If recovery succeeds, re-run the sync for that repo (the fast-forward branch); if it fails, return the original skip.

New helper in the same module:

```python
def _try_recover_stale_main(
    repo: RepoAudit,
    cfg: WorkspaceConfig,
    shell: ShellRunner,
    state_dir: Path,
) -> tuple[bool, str]:
    """Attempt to recover from a dirty-main-that-matches-upstream state.

    Returns (recovered, message). On `recovered=True`, the working tree is
    now at origin/<branch> with all stashed content verified redundant.
    On `recovered=False`, the working tree and index are restored to their
    pre-attempt state; the caller should proceed with the original skip.

    Always captures a diagnostic snapshot regardless of outcome.
    """
```

Algorithm — hash-compare dirty tracked files against `origin/<branch>`; reset if they all already match:

```
1. Capture diagnostic: capture_snapshot("sync", "dirty-main-pre-recovery", state_dir, repos={name: root}).
2. Behind check: `git rev-list --count <branch>..origin/<branch>` > 0.
   If not behind → return (False, "not behind origin; not the recoverable pattern").
3. Untracked check: `git ls-files --others --exclude-standard`.
   If any untracked present → return (False, "untracked files present; recovery skipped to preserve data").
   Rationale: the observed bug pattern involves only modified tracked files, not untracked adds.
   Refusing when untracked exist keeps the safety bar high.
4. Dirty-file enumeration: `git diff --name-only HEAD` → list of modified tracked files.
5. Per-file hash compare:
   For each file in the list:
     a. Compute working-tree blob hash: `git hash-object -- <file>`.
     b. Look up the same file's blob on origin/<branch>:
        `git rev-parse origin/<branch>:<file>` → gives the blob SHA if the file exists upstream.
        If the file does NOT exist on origin/<branch> (rev-parse fails) → the dirty state includes
        a file not in upstream. Not the recoverable pattern.
     c. If hashes match, file is redundant.
     d. If hashes differ, file contains real work not in upstream → return
        (False, "dirty file <name> does not match upstream; real user work").
        Capture a second diagnostic: capture_snapshot("sync", "dirty-main-real-user-work",
        ..., extra={"mismatched_file": <name>}).
6. All files matched upstream: safe to reset.
   `git checkout -- <file list>` → working tree is clean (just those tracked files restored).
7. Return (True, "recovered from stale main state").

The caller's existing behind-remote logic then runs `git pull --ff-only` and reports fast-forwarded.
```

Why hash-compare instead of stash:
- No state mutation until we've proven every dirty file is redundant. If we bail at step 5d, the working tree is untouched — user's real work stays exactly where it was.
- Simpler, more auditable. Diagnostic snapshots record the exact file names and hashes involved.
- Avoids stash/pop edge cases (stash apply conflicts, untracked file interactions).

Integration point in `_result_for`:

```python
def _result_for(repo: RepoAudit, cfg: WorkspaceConfig, shell: ShellRunner, state_dir: Path) -> SyncResult:
    blocking = [i for i in repo.issues if i.code in _BLOCKING_CODES]
    # New: attempt recovery only for the specific `dirty_worktree` solo case.
    if blocking and all(i.code == "dirty_worktree" for i in blocking):
        recovered, msg = _try_recover_stale_main(repo, cfg, shell, state_dir)
        if recovered:
            # Re-audit one-shot to get the post-recovery `behind_remote` info.
            # Simpler: we know a fast-forward just ran (part of recovery),
            # so mark as fast_forwarded with the recovery message.
            return SyncResult(repo.name, repo.path, "fast_forwarded", msg)
        # Recovery declined → fall through to the original skip.
    if blocking:
        first = blocking[0]
        return SyncResult(repo.name, repo.path, "skipped",
                          f"{first.code} — {first.message}")
    # ... rest unchanged (behind_remote fast-forward, up_to_date fallthrough) ...
```

Signature change: `_result_for` and `sync_repos` gain a `state_dir: Path` parameter plumbed through from the CLI (where `container.state_dir()` is already available).

### 3. Post-op sanity checks in `mship finish` and `mship close`

Both commands already accept a `container` and thus can read `container.state_dir()`. Before the CLI handler returns success:

```python
# At the end of finish (just before typer.Exit or successful return)
from mship.core.diagnostics import capture_snapshot

try:
    post_op_repos: dict[str, Path] = {}
    for repo_name in task.affected_repos:
        repo_path = config.repos[repo_name].path  # main checkout path, NOT worktree
        if repo_path and Path(repo_path).is_dir():
            post_op_repos[repo_name] = Path(repo_path).resolve()
    if post_op_repos:
        # Check any dirty state on main-checkout paths.
        any_dirty = False
        for name, path in post_op_repos.items():
            res = shell.run("git status --porcelain", cwd=path)
            if res.returncode == 0 and res.stdout.strip():
                any_dirty = True
                break
        if any_dirty:
            capture_snapshot(
                "finish", "dirty-main-post-op",
                container.state_dir(),
                repos=post_op_repos,
            )
except Exception:
    # Never let diagnostics failure break the command.
    pass
```

Same pattern for `close`. Guarded `try/except` around the entire block so diagnostics never break user workflows.

### 4. `mship doctor` row — edit `src/mship/core/doctor.py::DoctorChecker.diagnose`

Insert after existing checks:

```python
# Pending diagnostics (instrumentation captured anomalies)
diag_dir = self._state_dir / "diagnostics"
if diag_dir.is_dir():
    count = sum(1 for _ in diag_dir.glob("*.json"))
    if count > 0:
        report.checks.append(CheckResult(
            name="diagnostics",
            status="warn",
            message=(
                f"{count} snapshot(s) in .mothership/diagnostics/ "
                f"— review for unexpected-state captures; `rm -rf` to clear"
            ),
        ))
```

`DoctorChecker.__init__` already receives `state_dir` (verify; if not, add).

## Data flow

**Happy path (user's main is already clean):**

1. `mship sync` → audit → no blocking codes → fast-forward if behind.
2. No diagnostics captured, no recovery attempted, no behavior change.

**Stale-main bug recurs:**

1. `mship sync` → audit reports `dirty_worktree` on the mothership repo.
2. `_result_for` sees only `dirty_worktree` in blocking → calls `_try_recover_stale_main`.
3. Diagnostic snapshot written: `.mothership/diagnostics/<ts>-sync-dirty-main-pre-recovery.json`.
4. Recovery algorithm runs. Stash → ff → stash-show empty → drop stash.
5. `_result_for` returns `"fast_forwarded"` with the recovery message.
6. CLI prints `"mothership: fast-forwarded (recovered from stale main state)"`.

**Real user work on main (false-positive guard):**

1. User has real uncommitted edits on main that are NOT already in origin/main.
2. `mship sync` → audit reports `dirty_worktree`.
3. Recovery attempts stash → ff → stash-show non-empty → pop stash back.
4. Second diagnostic captured: `"dirty-main-real-user-work"`.
5. `_result_for` returns the original `"skipped"` with the original message.
6. User sees the existing refuse behavior. Their work is untouched.

**`mship finish` leaves main dirty (collecting evidence):**

1. Finish flow runs normally; PRs opened; `finished_at` stamped.
2. Post-op sanity check runs `git status --porcelain` on each affected repo's main path.
3. If dirty → diagnostic captured: `"finish-dirty-main-post-op"`.
4. Exit unchanged; user sees normal finish output.

**`mship doctor` after the bug was triggered once:**

1. Doctor iterates checks.
2. Finds `.mothership/diagnostics/2026-04-21T14-23-08Z-sync-dirty-main-pre-recovery.json`.
3. Prints `warn   diagnostics   1 snapshot(s) in .mothership/diagnostics/ — review for unexpected-state captures; \`rm -rf\` to clear`.

## Error handling

- **Snapshot write fails** (disk full, permissions, path too long): `capture_snapshot` catches `OSError`, returns None, logs at debug level. Caller continues.
- **`git hash-object` fails** (rare — file gone mid-run): recovery returns `(False, "hash-object failed: ...")`; no state mutation. Caller falls through to original skip.
- **`git rev-parse origin/<branch>:<file>` fails** (file not in upstream): recovery returns `(False, "dirty file <name> does not match upstream ...")`. State untouched; user preserved.
- **`git checkout -- <files>` fails during the reset phase** (after hashes verified): unexpected but possible (disk full, lock contention). Recovery logs a third diagnostic `"dirty-main-reset-failed"` and returns `(False, ...)`. Working tree may be in a partial-reset state; diagnostic captures enough to investigate.
- **Network partition on behind-check** (`git rev-list` doesn't hit network; it reads local refs): rev-list works offline. If `origin/<branch>` is stale on disk, the behind-check may miss updates. Acceptable — user can `git fetch` manually if needed.
- **Recovery is called from CI or non-interactive context**: same behavior, no prompts. If the caller needs to opt out later, add `--no-recover` then.

## Testing

### Unit — `tests/core/test_diagnostics.py` (new)

1. **Happy snapshot write.** `capture_snapshot("sync", "test", tmp_path)` creates `<tmp_path>/diagnostics/<ts>-sync-test.json` with all expected keys populated.
2. **Write failure is non-fatal.** `monkeypatch.setattr(Path, "write_text", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))`; `capture_snapshot(...)` returns None and doesn't raise.
3. **Filename is filesystem-safe.** Colons from the ISO timestamp are replaced with `-`.
4. **repos kwarg is populated.** Pass `repos={"r": /tmp/repo}`; snapshot contains per-repo git_status, head_sha, head_branch, reflog_tail.
5. **extra kwarg is included verbatim.** Pass `extra={"foo": "bar"}`; snapshot contains `"extra": {"foo": "bar"}`.

### Unit — `tests/core/test_sync_recovery.py` (new)

Each test sets up a `tmp_path` workspace with a real `file://` origin git repo so recovery can exercise real git commands.

1. **Happy path: dirty main matches upstream delta.** Origin has commits A→B; local main is at A; copy B's file content into the local working tree (dirty state matching upstream). Run `_try_recover_stale_main`. Assert returns `(True, "recovered ...")`, working tree is clean after the reset, one diagnostic file exists (pre-recovery).
2. **User work on main is preserved.** Origin at A→B; local main at A with a modification to a file whose content differs from B's version (real user work). Run recovery. Assert returns `(False, "dirty file ... does not match upstream ...")`, working tree still has the original edit (not reset), two diagnostic files exist (pre-recovery + real-user-work).
3. **Not behind origin.** Origin at A; local main at A with a dirty edit. Run recovery. Assert returns `(False, "not behind origin ...")`; no file hash comparison performed; one diagnostic captured.
4. **Untracked files present.** Origin A→B; local at A behind; local has a dirty tracked edit matching B AND one untracked file. Run recovery. Assert returns `(False, "untracked files present ...")`; tracked file is NOT reset (preserved); one diagnostic captured.
5. **Dirty file doesn't exist upstream.** Origin at A→B (where B doesn't introduce `foo.py`); local at A with a dirty modified `foo.py`. Run recovery. `git rev-parse origin/<branch>:foo.py` fails → returns `(False, "dirty file foo.py does not match upstream ...")`; file preserved; one or two diagnostics.
6. **Multiple dirty files all match.** Two files modified, both match their upstream blob. Assert returns `(True, ...)`, both files reset, working tree clean.
7. **Multiple dirty files, one mismatches.** Two files modified; first matches upstream, second doesn't. Assert returns `(False, ...)` mid-loop before touching either file (state mutation only happens after all files verified). Both files preserved.

### Unit — `tests/core/test_doctor.py` (extend)

6. **Doctor row when diagnostics dir has files.** Create `<state_dir>/diagnostics/fake.json`. Run doctor. Assert a `CheckResult(name="diagnostics", status="warn", ...)` is in the report with count=1.
7. **No row when diagnostics dir is empty or missing.** No `diagnostics` row in the report.

### Integration — `tests/cli/test_worktree.py` (extend for finish + close)

8. **`mship finish` captures post-op diagnostic when main is artificially dirtied.** Hook into the mock shell to write to a file in the main repo path during the finish flow. Run finish. Assert `.mothership/diagnostics/<ts>-finish-dirty-main-post-op.json` exists after the command.
9. **`mship finish` doesn't capture when main is clean.** Normal finish flow. Assert no diagnostic file appears.

### Integration — `tests/cli/test_sync.py` (if exists; else extend test_worktree)

10. **End-to-end recovery smoke.** `file://` origin workspace. Stage the dirty-main-matches-upstream pattern. Run `mship sync` via CliRunner. Assert exit 0, `fast-forwarded` in output, diagnostic file exists.

### Regression

- Existing `_result_for` tests stay green. The signature change adds `state_dir` — update existing callers/tests to pass it.
- `_BLOCKING_CODES` unchanged.
- Full `pytest tests/` green.

### No manual smoke

The diagnostic file mechanism and the stash-recovery are covered by unit + integration tests. Manual smoke would require triggering the actual mystery bug, which we can't do on-demand.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Hash-compare recovery, never `git reset --hard` and never stash | Per-file hash compare against `origin/<branch>` proves redundancy BEFORE mutating state. If any file doesn't match, we bail before touching anything. `reset --hard` or stash-based approaches mutate state first and depend on a reversal step that can fail. |
| 2 | Recovery only triggers when `dirty_worktree` is the SOLE blocking code | Prevents recovery from running when the repo also has `fetch_failed`, `diverged`, or similar — those cases need human intervention. |
| 3 | Two diagnostics per recovery attempt (pre-recovery + sad-path if user-work detected) | Pre-recovery snapshot captures the trigger state. Sad-path snapshot captures what user-work looked like, which is exactly what we need to narrow hypotheses. |
| 4 | No automatic rotation of diagnostics | Doctor surfaces count; user decides when to prune. Automation is premature before we know how often real captures fire. |
| 5 | No `mship diagnostics` subcommand | Files are JSON; `cat` works; the value of a CLI wrapper is tiny. Add later if pattern emerges. |
| 6 | Post-op checks in `mship finish` / `close` are silent | Data collection only. Surfacing a warning every time the bug fires would train users to ignore it (see #51). Doctor's row is the visible signal. |
| 7 | Doctor check uses `status=warn`, not `pass` or `fail` | Snapshots are anomalies worth looking at but not failures. Warn matches the signaling intent. |
| 8 | Recovery message uses `"recovered from stale main state"` (not `"stale-main-index bug"`) | User-facing; describes the observed behavior without committing to a specific root-cause narrative. |
