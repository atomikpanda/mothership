# First-Class Codex and OMP Hook Support Implementation Plan

**Spec:** `first-class-codex-hook-support`

**Goal:** Preserve Mothership's existing Claude lifecycle behavior while adding native project-local Codex and OMP integrations backed by one internal policy core.

**Architecture:** Add a private `core/lifecycle_hooks.py` owner for runtime-independent session context, multi-target edit policy, and inbox continuation decisions. Keep the existing hidden Claude CLI commands as adapters, extend them with an internal runtime selector, and add defensive Codex patch normalization. Install Codex configuration by structurally reconciling only Mothership command handlers in `.codex/hooks.json`. Install one deterministic Mothership-owned OMP extension at `.omp/extensions/mship.ts`; the extension invokes the hidden policy adapter over JSON stdin and translates decisions to OMP-native event results. Doctor validates artifacts and available runtime capabilities without making optional runtimes fatal.

**Constraints checked:**
- Existing owner search found `core/edit_guard.py::evaluate_edit`, `cli/internal.py::_session-context/_guard-edit/_drain`, `core/claude_settings.py`, `cli/init.py`, and `core/doctor.py`; reuse them rather than adding parallel policy behavior.
- No public hook SDK or separately packaged plugin.
- Adapter errors fail open with a warning; completed policy denials remain fail closed.
- Installation is deterministic, atomic where files are written, and preserves user-owned Codex keys/hooks plus sibling OMP extensions.

<!-- mship:task id=1 acs=ac6,ac7,ac9,ac10,ac12,ac15,ac19,ac21 -->
## Task 1: Shared lifecycle policy core

**Files:**
- Create `src/mship/core/lifecycle_hooks.py`
- Create `tests/core/test_lifecycle_hooks.py`
- Modify `src/mship/cli/internal.py`
- Update existing Claude adapter tests only where needed for parity assertions

1. Write failing policy tests for session context, complete multi-target guard evaluation, bypasses, one denied target among allowed targets, empty targets, inbox continuation, re-entry, unchanged inbox state, and store failures.
2. Implement private request/decision types that keep adapter failure distinct from allow/deny/context/continue/stop decisions.
3. Move orchestration—not the existing `evaluate_edit` decision algorithm—into the shared core. Evaluate every deduplicated normalized target before returning allow.
4. Move inbox listing, seen-cursor stamping, formatting, and bounded re-entry decision into the shared core.
5. Refactor `_session-context`, `_guard-edit`, and `_drain` to call the shared core while retaining their existing Claude stdout/stderr/exit contracts.
6. Run `uv run pytest tests/core/test_lifecycle_hooks.py tests/cli/test_guard_edit.py tests/cli/test_drain.py tests/cli/test_internal.py -q`.
<!-- /mship:task -->


<!-- mship:task id=2 acs=ac2,ac3,ac4,ac8,ac9,ac11,ac12,ac13,ac18 -->
## Task 2: Codex event normalization and installer

**Files:**
- Create `src/mship/core/codex_hooks.py`
- Create `tests/core/test_codex_hooks.py`
- Modify `src/mship/cli/internal.py`
- Modify `src/mship/cli/init.py`
- Create or extend `tests/cli/test_init_hooks.py`

1. Write failing fixtures for official Codex SessionStart, PreToolUse, and Stop payload/output contracts.
2. Write failing patch-target cases: update, add, delete, rename/move, multiple targets, duplicates, quoted/spaced paths, absolute paths, aliases, malformed patch, and one denied target.
3. Implement defensive Codex input normalization. `apply_patch`, `Edit`, and `Write` aliases must collect every represented target; ambiguous recognized edits become adapter errors rather than successful no-target evaluations.
4. Extend the hidden adapters with `--runtime codex`, preserving the existing no-option Claude behavior. Emit only documented Codex output or exit-code contracts.
5. Write failing installation tests for fresh creation, missing directories, idempotency, stale owned entries, unrelated keys/event handlers/order preservation, malformed JSON preservation, and atomic-write failure.
6. Implement `.codex/hooks.json` structural reconciliation using exact Mothership-owned command identities. Do not rewrite byte content when no semantic update is needed. Never replace malformed input.
7. Wire Codex installation into every existing agent-hook initialization path as best effort with explicit output.
8. Run `uv run pytest tests/core/test_codex_hooks.py tests/cli/test_init_hooks.py tests/core/test_claude_settings.py -q`.
<!-- /mship:task -->


<!-- mship:task id=3 acs=ac2,ac5,ac11,ac12,ac14,ac15,ac18 -->
## Task 3: OMP extension and installer

**Files:**
- Create `src/mship/core/omp_extension.py`
- Create `tests/core/test_omp_extension.py`
- Modify `src/mship/cli/internal.py`
- Modify `src/mship/cli/init.py`
- Extend initialization integration tests

1. Write failing installer tests for fresh creation, deterministic idempotency, stale artifact replacement, preserved sibling extensions, and atomic-write failure.
2. Add a hidden JSON-stdin adapter for OMP that accepts official `session_start`, `tool_call`, and `session_stop` fixtures and returns the private normalized decision envelope. Malformed payloads return an adapter-error envelope and allow the runtime action/stop.
3. Generate one deterministic `.omp/extensions/mship.ts` with an internal version marker. Register exactly `session_start`, `tool_call`, and `session_stop`.
4. In the extension, invoke `mship` with JSON stdin, catch process/parse/translation failures, log warnings without event contents, and fail open.
5. Translate context through `pi.sendMessage(..., { deliverAs: "nextTurn" })`, denials through `{ block: true, reason }`, and continuation through `{ continue: true, additionalContext }`.
6. Wire OMP installation into the same initialization flow without touching sibling extensions.
7. Run `uv run pytest tests/core/test_omp_extension.py tests/cli/test_init_hooks.py -q`.
<!-- /mship:task -->


<!-- mship:task id=4 acs=ac16,ac17 -->
## Task 4: Doctor diagnostics

**Files:**
- Modify `src/mship/core/doctor.py`
- Modify `tests/core/test_doctor.py`

1. Write failing checks for Claude/Codex/OMP installed, missing, malformed, stale, and missing event registration states.
2. Add runtime checks using injected `ShellRunner`/`shutil.which`: unavailable runtimes warn; detectable incompatible Codex hook capability and old OMP version warn; optional runtime failures never fail the report.
3. Always explain Codex project hook trust/review requirements without mutating trust state.
4. Reuse installer-owned constants and validators as each artifact's single source of truth.
5. Run `uv run pytest tests/core/test_doctor.py -q`.
<!-- /mship:task -->


<!-- mship:task id=5 acs=ac1,ac20,ac21,ac22 -->
## Task 5: Documentation and integration regression

**Files:**
- Modify `README.md`
- Modify `docs/cli.md`
- Modify relevant bundled workflow skill text only if it currently claims Claude-only installation
- Extend lifecycle installation integration tests

1. Document that `mship init` / `mship init --install-hooks` installs Git, Claude, Codex, and OMP project integrations; note Codex `/hooks` trust review and optional runtime warnings.
2. Add end-to-end installation assertions for all three runtime artifacts and lifecycle bindings.
3. Run the targeted lifecycle suite, then `mship test` so evidence is recorded on the task.
4. Run `mship doctor` in a fixture or current workspace and inspect exact runtime integration states.
5. Review `git diff --check` and the task-scoped diff; confirm no generated caches, user config, runtime binaries, or unrelated files are present.
<!-- /mship:task -->

<!-- mship:task id=6 acs=ac4,ac15,ac16 -->
## Task 6: Address verified Macroscope review findings

**Files:**
- Modify `src/mship/core/codex_hooks.py`
- Modify `src/mship/core/doctor.py`
- Modify `src/mship/core/agent_hooks.py`
- Modify `tests/core/test_codex_hooks.py`
- Modify `tests/core/test_doctor.py`
- Modify `tests/cli/test_drain.py`

**Interfaces:**
- Consumes `install_codex_hooks(Path) -> CodexInstallResult`, `DoctorChecker._agent_integration_checks(Path) -> list[CheckResult]`, and `stop(...) -> AgentHookDecision`.
- Preserves all runtime-facing decision envelopes and OMP's native `session_stop.stop_hook_active` ownership.

1. Add failing tests proving empty or whitespace-only existing Codex configuration is preserved as malformed input.
2. Add failing doctor tests proving zero configured Git roots produce no project-integration rows and unreadable/non-UTF-8 integration artifacts produce warnings instead of exceptions.
3. Add a failing drain test proving a thread with both an unanswered human reply and an unhandled agent event renders both messages and both action instructions.
4. Run the focused tests and confirm the new assertions fail for the reported reasons.
5. Parse every existing Codex configuration file, return no project-integration rows for zero roots, catch `OSError`/`UnicodeDecodeError` during doctor reads, and render dual-state threads in both drain sections using the relevant message for each state.
6. Run `uv run pytest tests/core/test_codex_hooks.py tests/core/test_doctor.py tests/cli/test_drain.py -q`.
7. Run `mship test`, commit and push the correction, reply to the four addressed threads, and resolve them.
8. Reply to the OMP continuation thread with the official `stop_hook_active` contract and the extension's unchanged event pass-through; do not duplicate continuation state in the extension.
<!-- /mship:task -->
