# Agent Harness Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `agent-harness-parity`

**Goal:** Make Mothership dispatch, setup, skills, context, and lifecycle enforcement portable across Claude Code, Codex, and OMP/Pi without weakening the shared policy in `core/agent_hooks.py`.

## Assumptions checked

- repo topology — **meta**; this WorkItem changes only the `mothership` member and does not alter cross-repo history or release coordination.
- credential locus — **not applicable**; no credential, token, relay, or egress behavior changes.
- execution locus — **both local and disposable workers**; all behavior is deterministic from checked-in code, temporary homes, and injected shell results, with optional local Codex/OMP probes used only as smoke evidence.
- state durability — **existing journal and SDD record**; resolved model values remain persisted in `DispatchRecord`, and no new session-only state is introduced.
- review surface — **terminal and async-neutral**; diagnostics are CLI `CheckResult` rows and dispatch contracts remain consumable by any controller.
- agent stream — **existing journal-backed behavior**; this change adds no stream or logging subsystem.
- dispatched model — **harness default unless explicitly overridden**; `inherit` is portable, while explicit values must be applied by a capable harness adapter or rejected with an actionable error.

**Architecture:** Keep `core/agent_hooks.py` as the only owner of session-start context, guarded-edit decisions, inbox continuation, and bounded stop re-entry. Keep model strings opaque in Mothership core: all built-in modes resolve to the sentinel `inherit`, meaning the dispatching harness uses its default model; explicit operator values remain byte-for-byte values interpreted only by a harness adapter. Consolidate Codex capability parsing in `core/codex_hooks.py` so setup and doctor report the same activation state. Install OMP/Pi skills through OMP's documented cross-runtime `~/.agents/skills/` discovery path, reusing the existing foreign-content-safe symlink machinery. Test runtime adapters as translations around the shared policy, not as independent policy implementations.

**Tech Stack:** Python 3.12, Typer, pytest, Markdown-based bundled skills, and Bun for generated OMP TypeScript smoke coverage.

## Global Constraints

- Do not modify `evaluate_edit`, WorkItem gating, inbox formatting/clearing, stop boundedness, fail-open policy, relay, GitHub, worktree, or PR behavior.
- Do not write Codex `config.toml`, enable experimental features, or approve project trust.
- Do not invent a Gemini-specific installer; Gemini remains on its documented native flow.
- Preserve atomic writes and safe-skip behavior for foreign files, directories, and symlinks.
- Do not add provider-name translation in Python. `sonnet`, `haiku`, `opus`, or any other operator override remains an opaque string.
- A runtime adapter may omit a selector only for `inherit`. If an explicit model cannot be selected by the available subagent API, it must stop before dispatch and print how to use `inherit` or a selector-capable tool.

---

<!-- mship:task id=1 acs=ac1,ac2,ac9 -->
## Task 1: Make dispatch model semantics portable

**Files:**
- Modify `src/mship/core/dispatch_models.py`
- Modify `tests/core/test_dispatch_models.py`
- Modify `tests/core/test_dispatch_stub.py`
- Modify `tests/cli/test_dispatch.py`
- Modify `src/mship/skills/subagent-driven-development/SKILL.md`
- Modify `src/mship/skills/subagent-driven-development/implementer-prompt.md`
- Modify `src/mship/skills/subagent-driven-development/task-reviewer-prompt.md`
- Modify `src/mship/skills/subagent-driven-development/re-review-prompt.md`
- Modify `src/mship/skills/using-mothership/references/codex-tools.md`
- Modify `src/mship/skills/using-mothership/references/pi-tools.md`
- Modify `src/mship/skills/working-with-mothership/SKILL.md`
- Modify `tests/skills/test_skill_dispatch_ergonomics.py`
- Modify `docs/configuration.md`

**Interfaces and invariants:**

```python
BUILTIN_MODEL_DEFAULTS: dict[str, str] = {
    "implementer": "inherit",
    "standalone": "inherit",
    "reviewer": "inherit",
}


def resolve_model(
    mode: str,
    *,
    flag: str | None,
    configured: dict[str, str] | None,
) -> str:
    """Return flag > configured value > portable built-in `inherit`."""
```

`DispatchRecord.model`, the closed stub's `model:` line, and the `emit:` line's model argument remain unchanged fields. An explicit value such as `openai/gpt-5.2` or `vendor/custom-tier` is stored and emitted verbatim. No core function validates, translates, or substitutes it.

- [ ] **Step 1: Write failing portable-default tests.**

  In `tests/core/test_dispatch_models.py`, replace the reviewer-specific expectation and cover all modes plus opaque overrides:

  ```python
  @pytest.mark.parametrize("mode", ["implementer", "standalone", "reviewer"])
  def test_builtin_defaults_inherit_harness_model(mode):
      assert resolve_model(mode, flag=None, configured=None) == "inherit"


  def test_operator_model_value_is_opaque_and_verbatim():
      value = "vendor/custom-tier:2026-08"
      assert resolve_model("reviewer", flag=None, configured={"reviewer": value}) == value
  ```

  In `tests/core/test_dispatch_stub.py`, build a record for each mode with `model="inherit"`; assert the closed field set remains `record`, `model`, `mode`, `worktree`, `emit`, and assert none of `sonnet`, `haiku`, or `opus` appears. Add one explicit-model record and assert the exact value appears in both model-bearing lines without replacement.

  In `tests/cli/test_dispatch.py`, change the no-override reviewer regression from `sonnet` to `inherit`, add implementer and standalone no-override cases if those modes are not already covered end-to-end, and retain the existing `--model haiku` precedence test as proof that an operator value is opaque.

- [ ] **Step 2: Run the model tests and confirm the reviewer case fails for the old default.**

  ```bash
  uv run pytest tests/core/test_dispatch_models.py tests/core/test_dispatch_stub.py tests/cli/test_dispatch.py -q
  ```

  Expected before implementation: the reviewer default and generated reviewer stub contain `sonnet`.

- [ ] **Step 3: Replace provider-specific defaults and comments in the core owner.**

  Update `dispatch_models.py` exactly as the interface above. Rewrite the module docstring to state:

  ```text
  `inherit` is a portable sentinel: the controller omits a model selector and
  lets the harness use its current/default model. Explicit CLI/config values
  are opaque operator choices passed through unchanged.
  ```

  Remove claims that an omitted selector necessarily chooses the most expensive tier and remove the cheaper-reviewer rationale. Keep precedence and unknown-mode validation unchanged.

- [ ] **Step 4: Make skill adapters implement the sentinel contract.**

  Replace every template instruction that always fills a `model:` field with this exact branching rule:

  ```text
  Read the stub's resolved model before dispatch:
  - `inherit`: omit the harness model selector; the harness default is intended.
  - any other value: pass it unchanged through a supported model selector.
  - if the available subagent API has no model selector, do not dispatch with
    an explicit value. Report: "mship resolved explicit model '<value>', but
    this subagent API cannot select a model; set this mode to inherit or use a
    selector-capable dispatch tool."
  Never translate one provider's model name into another.
  ```

  Apply the rule to implementer, task-reviewer, and re-review templates. In template-shaped examples, make the selector conditional rather than rendering `model: inherit` as a literal provider model:

  For an explicit resolved value, render the harness selector with that exact value:

  ```text
  model: vendor/custom-tier
  ```

  For `inherit`, omit the entire selector field; do not pass the literal string `inherit` to the provider.

  In `codex-tools.md`, document that `inherit` means invoking `spawn_agent` without a model selector; current Codex multi-agent APIs that expose no selector must reject explicit values with the exact actionable message above. In `pi-tools.md`, apply the same rule to the installed `subagent` tool: inspect its schema, pass explicit values only when it exposes a selector, otherwise reject. Claude's Task-style adapter continues to pass explicit values through its model field and omits that field for `inherit`.

  Update `working-with-mothership/SKILL.md` and the model-selection section of `subagent-driven-development/SKILL.md` to describe portable defaults, opaque overrides, and the reject-not-substitute rule. Remove provider-tier assumptions from Mothership-workspace dispatch; retain generic task-complexity guidance only for non-Mothership work where the controller chooses a model itself.

- [ ] **Step 5: Add contract tests for generated skill instructions.**

  In `tests/skills/test_skill_dispatch_ergonomics.py`, load the affected bundled Markdown and assert:

  ```python
  assert "`inherit`" in text
  assert "omit" in text
  assert "cannot select a model" in text
  assert "Never translate" in text
  ```

  Add a focused scan over the Mothership dispatch sections and prompt templates—not historical plans or vendor ledgers—proving that default instructions do not prescribe `sonnet`, `haiku`, or `opus`. Assert Codex and Pi references contain both branches: selector application when available and actionable rejection when unavailable.

- [ ] **Step 6: Update configuration documentation.**

  Change `docs/configuration.md` to say that every built-in mode defaults to `inherit`, define the sentinel as harness-default behavior, and state that configured values are emitted verbatim and require a selector-capable harness adapter. Do not list provider model names as defaults.

- [ ] **Step 7: Verify, commit, and journal Task 1.**

  ```bash
  uv run pytest tests/core/test_dispatch_models.py tests/core/test_dispatch_stub.py tests/cli/test_dispatch.py tests/skills/test_skill_dispatch_ergonomics.py -q
  mship test --task agent-harness-parity
  mship commit "feat: make dispatch model defaults portable" --task agent-harness-parity
  mship journal "portable inherit defaults and explicit model adapter rules implemented; dispatch and skill contract tests passing" --task agent-harness-parity --action committed
  ```
<!-- /mship:task -->

<!-- mship:task id=2 acs=ac3,ac4,ac9 -->
## Task 2: Consolidate Codex activation diagnostics

**Files:**
- Modify `src/mship/core/codex_hooks.py`
- Modify `src/mship/cli/init.py`
- Modify `src/mship/core/doctor.py`
- Modify `tests/core/test_codex_hooks.py`
- Modify `tests/cli/test_init.py`
- Modify `tests/core/test_doctor.py`
- Modify `docs/cli.md`

**Interfaces and states:**

```python
CODEX_FEATURE_ENABLE_COMMAND = "codex features enable codex_hooks"
CODEX_TRUST_ACTION = "open `/hooks` in Codex to review and trust the project hooks"
CODEX_CAPABILITY_PROBE_TIMEOUT_SECONDS = 5


class CodexHookCapability(str, Enum):
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    ENABLED = "enabled"
    TIMED_OUT = "timed-out"


@dataclass(frozen=True)
class CodexHookCapabilityResult:
    state: CodexHookCapability
    feature_name: str | None = None
    detail: str = ""


def probe_codex_hook_capability(
    shell: ShellRunner,
    cwd: Path,
    *,
    codex_binary: str | None,
) -> CodexHookCapabilityResult:
    """Run and parse `codex features list` without mutating Codex config/trust."""
```

Accept both feature row names observed across supported versions: `codex_hooks` and `hooks`. Treat a successful row ending in `true` as enabled, a successful row ending in `false` as disabled, missing/unparseable rows or a nonzero exit as unavailable, timeout as timed out, and a missing binary as absent. The probe never executes `features enable` and never opens or approves `/hooks`.

- [ ] **Step 1: Write failing probe-table tests.**

  In `tests/core/test_codex_hooks.py`, parameterize exact `ShellResult` fixtures:

  ```python
  @pytest.mark.parametrize(
      ("stdout", "returncode", "expected"),
      [
          ("codex_hooks  under-development  false\n", 0, CodexHookCapability.DISABLED),
          ("hooks experimental true\n", 0, CodexHookCapability.ENABLED),
          ("multi_agent experimental true\n", 0, CodexHookCapability.UNAVAILABLE),
          ("", 1, CodexHookCapability.UNAVAILABLE),
      ],
  )
  def test_probe_codex_hook_capability(stdout, returncode, expected):
      shell = Mock()
      shell.run.return_value = SimpleNamespace(
          stdout=stdout,
          stderr="",
          returncode=returncode,
      )

      result = probe_codex_hook_capability(
          shell,
          Path("/workspace"),
          codex_binary="/usr/bin/codex",
      )

      assert result.state is expected
      shell.run.assert_called_once_with(
          "codex features list",
          cwd=Path("/workspace"),
          timeout=CODEX_CAPABILITY_PROBE_TIMEOUT_SECONDS,
      )
  ```

  Add missing-binary and `subprocess.TimeoutExpired` tests. Assert the shell call is exactly `codex features list`, uses the shared timeout constant, and performs no second command.

- [ ] **Step 2: Write failing init and doctor state-matrix tests.**

  Extend `tests/cli/test_init.py` so `mship init --install-hooks` with current Codex hook files reports:

  - disabled/unavailable: `configured but inactive`, the exact command ``codex features enable codex_hooks``, and the exact `/hooks` trust action;
  - enabled: `configured; capability enabled; trust still required`, with `/hooks`, and no claim that hooks are active;
  - absent/timed out: configured but not verified active, with an install/timeout detail and no mutation.

  Extend `tests/core/test_doctor.py` with the same matrix. For current registration plus enabled capability, assert `agent-runtime/codex` may pass but `agent-hooks/codex` remains a warning until manual trust, and no result message contains `fully active`. For disabled/unavailable capability, assert both the enable command and `/hooks` action appear. Snapshot the fake home/config before and after each command and assert byte equality outside the expected project hook artifact.

- [ ] **Step 3: Run the focused tests and observe setup/doctor disagreement.**

  ```bash
  uv run pytest tests/core/test_codex_hooks.py tests/cli/test_init.py tests/core/test_doctor.py -q
  ```

  Expected before implementation: init only reports file installation; doctor independently parses capability rows and can call current registration a pass.

- [ ] **Step 4: Implement the shared capability probe in `codex_hooks.py`.**

  Add the enum, result dataclass, constants, and probe signature above beside the existing Codex artifact constants. Parse output without regular-expression inference beyond splitting non-empty lines; compare the first token to the accepted names and the final token to `true`/`false`. Preserve `install_codex_hooks` and `registration_issues` behavior unchanged.

- [ ] **Step 5: Use the probe after installation and in doctor.**

  In `_install_agent_hooks_with_output`, after all per-project Codex artifacts are reconciled, call the shared probe once for the workspace root. Print one warning that describes the aggregate activation state; do not run a probe per repo. Continue printing each artifact's installed/updated/skipped line.

  In `DoctorChecker._agent_integration_checks`, remove the local `features` parsing loop and call the shared probe with the injected `ShellRunner`. Build rows from the result:

  ```text
  agent-runtime/codex: enabled capability may pass; all other states warn.
  agent-hooks/codex: malformed/missing/stale registration warns with reinstall action.
  agent-hooks/codex: current registration warns that it is configured but trust is
  unverified, and prints the /hooks action; it must not pass as active.
  ```

  Disabled and unavailable states must include both `codex features enable codex_hooks` and `/hooks`. Enabled state must include only the remaining `/hooks` action. Keep optional runtime absence nonfatal.

- [ ] **Step 6: Document the manual boundary.**

  In `docs/cli.md`, state that `mship init --install-hooks` writes project hook files but does not enable Codex features or trust the project. Include the two explicit operator actions and say `mship doctor` distinguishes artifact validity, feature capability, and unresolved trust.

- [ ] **Step 7: Verify no Codex config/trust mutation, then commit and journal.**

  ```bash
  uv run pytest tests/core/test_codex_hooks.py tests/cli/test_init.py tests/core/test_doctor.py -q
  codex features list
  mship test --task agent-harness-parity
  mship commit "feat: report Codex hook activation state" --task agent-harness-parity
  mship journal "Codex setup and doctor now share one read-only capability probe and distinguish configured, enabled, and untrusted states" --task agent-harness-parity --action committed
  ```

  The live `codex features list` command is observational only. Record its actual `codex_hooks`/`hooks` row in the journal; do not enable it as part of verification.
<!-- /mship:task -->

<!-- mship:task id=3 acs=ac5,ac6,ac7,ac9 -->
## Task 3: Add first-class OMP/Pi skills and context

**Files:**
- Modify `src/mship/core/skill_install.py`
- Modify `src/mship/cli/skill.py`
- Modify `src/mship/core/doctor.py`
- Modify `src/mship/core/context.py`
- Modify `tests/core/test_skill_install.py`
- Modify `tests/cli/test_skill.py`
- Modify `tests/core/test_doctor.py`
- Modify `tests/cli/test_context.py`
- Modify `README.md`
- Modify `docs/cli.md`

**Discovery contract:** OMP's native `agents` discovery provider scans user-level `.agents/skills` and `.agent/skills`. Use the cross-runtime canonical target already used by Codex:

```python
SHARED_AGENT_SKILLS_TARGET = Path(".agents") / "skills" / "mothership"


def shared_agent_skills_target(home: Path | None = None) -> Path:
    return (home or Path.home()) / SHARED_AGENT_SKILLS_TARGET
```

Both Codex and OMP installers point the directory-level target at `pkg_skills_source()`. `pi` is a CLI alias normalized to canonical agent name `omp`; it must not produce a second install or health identity.

- [ ] **Step 1: Write failing installer and alias tests.**

  In `tests/core/test_skill_install.py`, add OMP cases proving:

  ```python
  result = install_for_omp()
  assert result.agent == "omp"
  assert result.dest == home / ".agents" / "skills"
  assert (result.dest / "mothership").resolve() == pkg_skills_source().resolve()
  assert {p.name for p in (result.dest / "mothership").iterdir() if (p / "SKILL.md").is_file()} == bundled_names
  ```

  Save `os.readlink(result.dest / "mothership")`, run the installer again, and assert the link text and target tree bytes are unchanged. Reuse the existing collision table to prove foreign symlink/file/directory entries are skipped without `--force`, an owned dangling link is repaired, and `--force` replaces foreign content only when explicitly requested.

  In `tests/cli/test_skill.py`, assert `--only omp` and `--only pi` each produce one canonical `omp` result, `--only codex,omp` does not corrupt or duplicate the shared target, unknown agent validation remains strict, and auto-detection selects OMP when `omp`/`pi` is on PATH or `~/.omp` exists. Update help/list expectations to include `omp` and the `pi` alias.

- [ ] **Step 2: Write failing doctor and context tests.**

  In `tests/core/test_doctor.py`, enable OMP detection and assert a separate `skills/omp` row for current, missing, owned-dangling, stale, and foreign shared targets. Every non-current result must name `mship skill install --only omp`; foreign content must add `--force`. Assert this row is independent of `agent-hooks/omp` and `agent-runtime/omp`.

  In `tests/cli/test_context.py`, parameterize `claude-code`, `codex`, and `omp` and assert their `audience.instructions` values are identical implementer text. Keep explicit negative cases for `--for pi`, `--for unknown`, and `--kind` with a non-reviewer audience unless the public alias is intentionally documented for context; the approved contract adds only canonical `omp`.

- [ ] **Step 3: Run focused tests and confirm OMP is currently rejected.**

  ```bash
  uv run pytest tests/core/test_skill_install.py tests/cli/test_skill.py tests/core/test_doctor.py tests/cli/test_context.py -q
  ```

  Expected before implementation: `omp`/`pi` are unknown install targets, no `skills/omp` row exists, and `mship context --for omp` fails validation.

- [ ] **Step 4: Implement the shared discovery target and OMP installer.**

  Replace the Codex-specific target construction with `shared_agent_skills_target()`. Keep `refresh_symlink` as the only collision writer. Implement:

  ```python
  def install_for_omp(*, force: bool = False) -> AgentInstallResult:
      src = pkg_skills_source()
      target = shared_agent_skills_target()
      outcome = refresh_symlink(src, target, force=force)
      return AgentInstallResult(
          agent="omp",
          dest=target.parent,
          count=0 if outcome is RefreshOutcome.skipped else 1,
          skipped=["mothership"] if outcome is RefreshOutcome.skipped else [],
          replaced=["mothership"] if outcome is RefreshOutcome.replaced else [],
      )
  ```

  Refactor `install_for_codex` to use the same target helper without changing its result shape. Add OMP to `_detect_agents` using `omp`, the legacy `pi` executable, or `~/.omp`; do not add Gemini installation behavior.

- [ ] **Step 5: Normalize CLI names and preserve one canonical result.**

  Extend `SUPPORTED_AGENTS` and `_INSTALLERS` with `omp`. Normalize requested names before sorting/deduplicating:

  ```python
  aliases = {"pi": "omp"}
  targets = sorted({aliases.get(name, name) for name in wanted})
  ```

  Validate against the union of canonical names and aliases before normalization. The help text must say `pi` is an alias for `omp`. If both Codex and OMP are selected, each result may report the same discovery target, but the second install must be a byte-idempotent no-op; never create a second private skill copy.

- [ ] **Step 6: Add OMP skill health and context audience.**

  In `doctor.py`, rename the private Codex-only target helper to the shared target owner and use the existing directory-level installed/dangling/foreign classifier for both detected Codex and OMP. Pass an agent-specific repair command into `_format_skill_check` so `skills/omp` recommends `mship skill install --only omp` while existing Claude/Codex guidance remains correct.

  In `context.py`, add `"omp"` to `FOR_VALUES` and map `("omp", None)` to `_IMPLEMENTER_INSTRUCTIONS`. Do not duplicate the instruction string or add a new OMP-specific variant.

- [ ] **Step 7: Update user-facing installation documentation.**

  Update `README.md` and `docs/cli.md` command syntax to include `omp` and canonical Pi alias `pi`. State that Codex and OMP share the `.agents/skills/mothership` link, foreign entries are safe-skipped, and `mship doctor` reports OMP skill availability separately from project lifecycle extension compatibility.

- [ ] **Step 8: Smoke-test in an isolated home, then commit and journal.**

  ```bash
  uv run pytest tests/core/test_skill_install.py tests/cli/test_skill.py tests/core/test_doctor.py tests/cli/test_context.py -q
  env HOME=/tmp/mship-omp-skill-smoke uv run mship skill install --only omp --yes
  env HOME=/tmp/mship-omp-skill-smoke uv run mship skill install --only pi --yes
  uv run mship context --for omp --task agent-harness-parity
  mship test --task agent-harness-parity
  mship commit "feat: add OMP skill and context support" --task agent-harness-parity
  mship journal "OMP/Pi skill install, independent doctor health, and canonical omp context audience implemented; isolated-home smoke passed" --task agent-harness-parity --action committed
  ```

  Remove `/tmp/mship-omp-skill-smoke` after confirming the second install leaves the symlink text and target bytes unchanged.
<!-- /mship:task -->

<!-- mship:task id=4 acs=ac8,ac9 -->
## Task 4: Prove cross-runtime lifecycle conformance

**Files:**
- Modify `src/mship/core/claude_settings.py`
- Modify `src/mship/cli/internal.py`
- Modify `tests/core/test_claude_settings.py`
- Create `tests/cli/test_agent_hook_conformance.py`
- Modify `tests/cli/test_omp_hook.py`
- Modify `tests/core/test_omp_extension.py`
- Modify `tests/core/test_codex_hooks.py`
- Modify `tests/core/test_agent_hooks.py`

**Conformance boundary:** Tests feed runtime-native fixtures into each adapter, normalize the adapter result to `context | allow | deny | continue | stop | error`, and compare semantic kind/message. The tests must call the real hidden CLI adapters; mocking `agent_hooks.py` alone does not prove payload extraction or native result translation.

- [ ] **Step 1: Add failing Claude multi-target extraction tests.**

  Add this owner beside Claude registration logic, not in shared policy:

  ```python
  def extract_claude_edit_targets(event: dict[str, Any]) -> tuple[str, ...]:
      """Extract every path represented by Claude Edit/Write/MultiEdit/NotebookEdit."""
  ```

  In `tests/core/test_claude_settings.py`, cover `file_path`, `notebook_path`, `edits: [{"file_path": "src/a.py"}, {"file_path": "src/b.py"}]`, duplicates, mixed valid/invalid entries, unsupported tools returning `()`, and recognized edit payloads with no path raising `ValueError`. A MultiEdit fixture containing one allowed worktree path and one main-checkout path must expose both targets.

- [ ] **Step 2: Create adapter-fixture helpers and failing conformance cases.**

  In `tests/cli/test_agent_hook_conformance.py`, define three small test-only invokers:

  ```python
  ADAPTERS = {
      "claude": invoke_claude_hidden_commands,
      "codex": invoke_codex_hidden_commands,
      "omp": invoke_omp_hidden_command,
  }
  ```

  Each invoker translates its native CLI stdout/stderr/exit code back to a test-only `{kind, message}` structure. Reuse one bootstrapped workspace/state/message store for these parameterized contracts:

  1. session start with no active task returns the same actionable context text;
  2. a valid worktree edit allows;
  3. any denied target in a multi-target fixture denies the whole tool call and names the denied path/reason;
  4. `MSHIP_ALLOW_MAIN_EDIT=1` allows the same main-checkout fixture for all runtimes;
  5. `MSHIP_BYPASS_GATE=1` bypasses only the WorkItem clearance gate while preserving main-checkout protection;
  6. an awaiting human reply or agent event returns `continue` with `mship reply`/action instructions;
  7. the same stop event with continuation-active state returns `stop` and does not loop;
  8. a forced adapter exception fails open and reports a warning naming runtime and lifecycle event.

  Assert semantic equivalence, not identical native JSON shape. Claude/Codex exit codes, Codex JSON, and OMP decision envelopes remain runtime-native.

- [ ] **Step 3: Run conformance tests and capture the current failures.**

  ```bash
  uv run pytest tests/core/test_claude_settings.py tests/cli/test_agent_hook_conformance.py tests/cli/test_omp_hook.py -q
  ```

  Expected before implementation: Claude does not extract MultiEdit target lists and suppresses adapter failure warnings while Codex/OMP report them.

- [ ] **Step 4: Route Claude extraction through its adapter owner and align failure reporting.**

  In `_guard-edit`, call `extract_claude_edit_targets` for `Runtime.CLAUDE` instead of reading a single `tool_input.file_path`. Preserve Codex extraction and OMP extraction unchanged. Pass every extracted target to `agent_hooks.pre_tool_use`; do not alter `_normalize_targets` or `evaluate_edit`.

  Remove the Claude-only stderr suppression in `_guard-edit`, `_drain`, and `_session-context`. Every caught adapter exception must print:

  ```text
  mship: <runtime> <lifecycle-event> adapter failed open
  ```

  and preserve the existing fail-open native result. Never print event bodies, file contents, messages, or exception text.

- [ ] **Step 5: Exercise the generated OMP extension under Bun.**

  Extend `tests/core/test_omp_extension.py` with a behavioral smoke test skipped only when `bun` is unavailable. Install `OMP_EXTENSION_SOURCE` into a temporary project, put a fake `mship` executable first on `PATH`, and import the generated TypeScript from a Bun harness whose fake `pi` records `on`, `sendMessage`, and `logger.warn` calls. Drive the registered handlers with adapter outputs for context, deny, continue, stop, invalid JSON, and nonzero exit. Assert:

  ```text
  session_start context -> sendMessage(
      {customType: "mship-session-context", content: "context", display: false},
      {deliverAs: "nextTurn"},
  )
  tool_call deny         -> {block: true, reason: "denied"}
  session_stop continue  -> {continue: true, additionalContext: "answer inbox"}
  adapter error          -> warning and no block/forced continuation
  ```

  Assert exactly `session_start`, `tool_call`, and `session_stop` are registered. Keep the existing deterministic source, sibling-extension preservation, stale replacement, and atomic-write tests.

- [ ] **Step 6: Run the complete focused parity suite.**

  ```bash
  uv run pytest \
    tests/core/test_dispatch_models.py \
    tests/core/test_dispatch_stub.py \
    tests/cli/test_dispatch.py \
    tests/core/test_codex_hooks.py \
    tests/cli/test_init.py \
    tests/core/test_skill_install.py \
    tests/cli/test_skill.py \
    tests/core/test_context.py \
    tests/cli/test_context.py \
    tests/core/test_doctor.py \
    tests/core/test_agent_hooks.py \
    tests/core/test_claude_settings.py \
    tests/core/test_omp_extension.py \
    tests/cli/test_internal.py \
    tests/cli/test_drain.py \
    tests/cli/test_omp_hook.py \
    tests/cli/test_agent_hook_conformance.py \
    tests/skills/test_skill_dispatch_ergonomics.py -q
  ```

  All existing Claude, Codex, and OMP lifecycle tests must remain green. Test failures may be fixed only in adapter extraction/translation or test fixtures unless they expose a pre-existing shared-policy bug separately approved by the user.

- [ ] **Step 7: Run live read-only runtime probes and final smoke.**

  ```bash
  codex features list
  omp --version
  uv run mship doctor
  uv run mship context --for omp --task agent-harness-parity
  mship test --task agent-harness-parity
  ```

  Compare `mship doctor` with the actual Codex feature row and OMP version. Do not enable Codex hooks or alter trust. If either binary is unavailable, record that limitation and rely on the injected capability/version tests plus Bun extension smoke; do not claim a live probe ran.

- [ ] **Step 8: Review scope, commit, and journal Task 4.**

  ```bash
  mship commit "test: enforce agent harness lifecycle parity" --task agent-harness-parity
  mship journal "cross-runtime fixtures cover session context, multi-target guards, bypasses, inbox continuation, bounded stop, failure reporting, and generated OMP extension execution" --task agent-harness-parity --action committed
  ```

  Inspect the task-scoped change and confirm there are no user Codex config changes, trust state, generated caches, temporary homes, relay changes, provider translation tables, or Ground Control files.
<!-- /mship:task -->
