# `mship spec draft` / `apply` / `validate` Implementation Plan (MOS-146)

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the drafting layer on top of the A1 Spec substrate — `mship spec draft` (emit a drafting prompt), `mship spec apply` (ingest the model's structured JSON), and `mship spec validate` (enforce the structured-markdown contract) — without mship ever calling a model.

**Architecture:** Markdown-canonical specs stay the source of truth (A1, merged). `draft` emits an agent-agnostic prompt to stdout (mirroring `core/dispatch.py`); the caller runs its model; `apply` ingests a `SpecDraft` JSON, renders the canonical body + sets structured fields + assigns `ac/q` ids + advances to `needs_review`. `validate` enforces frontmatter (pydantic) + canonical body sections. Design: [docs/superpowers/specs/2026-06-14-spec-draft-design.md](../specs/2026-06-14-spec-draft-design.md).

**Tech Stack:** Python, pydantic v2, PyYAML, Typer, pytest. Builds on `core/spec.py`, `core/spec_store.py` (both from MOS-145, in `main`).

---

## File structure

- **Modify** `src/mship/core/spec.py` — add the `SpecDraft` model.
- **Create** `src/mship/core/spec_body.py` — `render_body` / `parse_body_sections` / `validate_body_structure` + `REQUIRED_SECTIONS`. One responsibility: the canonical body contract.
- **Create** `src/mship/core/spec_draft.py` — `build_draft_prompt` (the emitted prompt) + `apply_draft` (merge a `SpecDraft` into a `Spec`). Mirrors `core/dispatch.py`.
- **Modify** `src/mship/cli/spec.py` — add `draft`, `apply`, `validate` commands to the existing `spec` sub-app.
- **Test** `tests/core/test_spec.py` (extend — SpecDraft), `tests/core/test_spec_body.py` (create), `tests/core/test_spec_draft.py` (create), `tests/cli/test_spec.py` (extend — the 3 commands).

Run tests with `uv run pytest <path>`; full suite via `mship test`.

---

## Task 1: `SpecDraft` model

**Files:** Modify `src/mship/core/spec.py`; Test `tests/core/test_spec.py`.

- [ ] **Step 1: Write the failing test** (append to `tests/core/test_spec.py`)

```python
def test_spec_draft_defaults():
    from mship.core.spec import SpecDraft
    d = SpecDraft(problem="p", user_story="u", approach="a")
    assert d.non_goals == []
    assert d.risks == []
    assert d.affected_repos == []
    assert d.acceptance_criteria == []
    assert d.open_questions == []


def test_spec_draft_accepts_lists():
    from mship.core.spec import SpecDraft
    d = SpecDraft(
        problem="p", user_story="u", approach="a",
        acceptance_criteria=["c1", "c2"], open_questions=["q1"],
        non_goals=["ng"], risks=["r"], affected_repos=["mothership"],
    )
    assert d.acceptance_criteria == ["c1", "c2"]
    assert d.open_questions == ["q1"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/core/test_spec.py -k spec_draft -v`
Expected: FAIL — `ImportError: cannot import name 'SpecDraft'`.

- [ ] **Step 3: Implement** (append to `src/mship/core/spec.py`)

```python
class SpecDraft(BaseModel):
    """The draftable subset a model produces; ingested by `mship spec apply`.

    Criteria/questions are plain text — mship assigns their ids on apply.
    """
    problem: str
    user_story: str
    approach: str
    non_goals: list[str] = []
    risks: list[str] = []
    affected_repos: list[str] = []
    acceptance_criteria: list[str] = []
    open_questions: list[str] = []
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/core/test_spec.py -v`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/spec.py tests/core/test_spec.py
git commit -m "feat(spec): add SpecDraft model (draftable subset)"
mship journal "added SpecDraft model for the draft/apply flow; tests passing" --action committed
```

---

## Task 2: Body contract helpers

**Files:** Create `src/mship/core/spec_body.py`; Test `tests/core/test_spec_body.py`.

- [ ] **Step 1: Write the failing test** (`tests/core/test_spec_body.py`)

```python
from mship.core.spec_body import (
    REQUIRED_SECTIONS, render_body, parse_body_sections, validate_body_structure,
)


def test_render_body_has_all_sections():
    body = render_body("the problem", "as a user...", "the approach")
    for section in REQUIRED_SECTIONS:
        assert f"## {section}" in body
    assert "the problem" in body and "the approach" in body


def test_parse_round_trips_rendered_body():
    body = render_body("P", "U", "A")
    sections = parse_body_sections(body)
    assert sections["Problem"] == "P"
    assert sections["User story"] == "U"
    assert sections["Approach"] == "A"


def test_validate_flags_missing_sections():
    assert validate_body_structure(render_body("p", "u", "a")) == []
    missing = validate_body_structure("## Problem\n\nonly problem\n")
    assert "User story" in missing and "Approach" in missing
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/core/test_spec_body.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.spec_body'`.

- [ ] **Step 3: Implement** (`src/mship/core/spec_body.py`)

```python
from __future__ import annotations

REQUIRED_SECTIONS: tuple[str, ...] = ("Problem", "User story", "Approach")


def render_body(problem: str, user_story: str, approach: str) -> str:
    return (
        f"## Problem\n\n{problem.strip()}\n\n"
        f"## User story\n\n{user_story.strip()}\n\n"
        f"## Approach\n\n{approach.strip()}\n"
    )


def parse_body_sections(body: str) -> dict[str, str]:
    """Split a markdown body into {section-heading: prose} by `## ` headings."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def validate_body_structure(body: str) -> list[str]:
    """Return the names of any REQUIRED_SECTIONS missing from `body` (empty = ok)."""
    present = parse_body_sections(body)
    return [s for s in REQUIRED_SECTIONS if s not in present]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/core/test_spec_body.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/spec_body.py tests/core/test_spec_body.py
git commit -m "feat(spec): canonical body render/parse/validate helpers"
mship journal "spec_body: render + parse + validate canonical sections; tests passing" --action committed
```

---

## Task 3: `build_draft_prompt`

**Files:** Create `src/mship/core/spec_draft.py`; Test `tests/core/test_spec_draft.py`.

- [ ] **Step 1: Write the failing test** (`tests/core/test_spec_draft.py`)

```python
from mship.core.spec_draft import build_draft_prompt


def test_build_draft_prompt_contains_intent_schema_and_apply():
    prompt = build_draft_prompt("decision-queue", "I want X away from the desk")
    assert "decision-queue" in prompt
    assert "I want X away from the desk" in prompt          # the intent
    assert "acceptance_criteria" in prompt                  # the JSON shape
    assert "open_questions" in prompt
    assert "mship spec apply decision-queue --from-json" in prompt  # how to apply
    assert "only" in prompt.lower()                         # "output only JSON"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/core/test_spec_draft.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.spec_draft'`.

- [ ] **Step 3: Implement** (`src/mship/core/spec_draft.py`)

```python
from __future__ import annotations

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec, SpecDraft
from mship.core.spec_body import render_body


def build_draft_prompt(spec_id: str, intent_text: str) -> str:
    """Agent-agnostic prompt: turn intent into a SpecDraft JSON. No model call."""
    return f"""\
# Draft spec `{spec_id}`

Turn the intent below into a structured spec. Output **only** a single JSON
object matching this shape — no prose, no markdown fence:

{{
  "problem": "<why this matters>",
  "user_story": "As a <user>, I want <capability>, so that <benefit>.",
  "approach": "<how it works; key decisions>",
  "non_goals": ["<explicitly out of scope>"],
  "risks": ["<known risk>"],
  "affected_repos": ["<repo>"],
  "acceptance_criteria": ["<testable, user-visible outcome>"],
  "open_questions": ["<must be resolved before approval>"]
}}

## Intent

{intent_text}

## Apply your result

Save the JSON to a file and run:

    mship spec apply {spec_id} --from-json <file>

or pipe it directly:

    cat draft.json | mship spec apply {spec_id} --from-json -
"""
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/core/test_spec_draft.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/spec_draft.py tests/core/test_spec_draft.py
git commit -m "feat(spec): build_draft_prompt (emit drafting prompt, no model call)"
mship journal "spec_draft.build_draft_prompt emits agent-agnostic drafting prompt" --action committed
```

---

## Task 4: `apply_draft` merge

**Files:** Modify `src/mship/core/spec_draft.py`; Test `tests/core/test_spec_draft.py`.

- [ ] **Step 1: Write the failing test** (append to `tests/core/test_spec_draft.py`)

```python
from datetime import datetime, timezone

from mship.core.spec import Spec, SpecDraft
from mship.core.spec_draft import apply_draft
from mship.core.spec_body import validate_body_structure


def _spec():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    return Spec(id="dq", title="DQ", status="drafting", created_at=now, updated_at=now,
                task_slug="dq")


def test_apply_draft_merges_fields_and_assigns_ids():
    spec = _spec()
    draft = SpecDraft(
        problem="P", user_story="U", approach="A",
        non_goals=["chat"], risks=["scope"], affected_repos=["mothership"],
        acceptance_criteria=["view questions", "record answer"],
        open_questions=["Android in v0?"],
    )
    out = apply_draft(spec, draft)
    assert validate_body_structure(out.body) == []          # canonical body rendered
    assert out.non_goals == ["chat"] and out.affected_repos == ["mothership"]
    assert [c.id for c in out.acceptance_criteria] == ["ac1", "ac2"]
    assert out.acceptance_criteria[0].text == "view questions"
    assert out.acceptance_criteria[0].verdict == "unreviewed"
    assert [q.id for q in out.open_questions] == ["q1"]
    assert out.open_questions[0].answer is None
    # identity preserved
    assert out.id == "dq" and out.task_slug == "dq"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/core/test_spec_draft.py -k apply_draft -v`
Expected: FAIL — `ImportError: cannot import name 'apply_draft'`.

- [ ] **Step 3: Implement** (append to `src/mship/core/spec_draft.py`)

```python
def apply_draft(spec: Spec, draft: SpecDraft) -> Spec:
    """Merge a SpecDraft into `spec` in place: render the canonical body, set the
    structured fields, and assign deterministic `ac`/`q` ids. Does NOT change
    status/updated_at — the caller owns the lifecycle transition + persistence."""
    spec.body = render_body(draft.problem, draft.user_story, draft.approach)
    spec.non_goals = list(draft.non_goals)
    spec.risks = list(draft.risks)
    spec.affected_repos = list(draft.affected_repos)
    spec.acceptance_criteria = [
        AcceptanceCriterion(id=f"ac{i + 1}", text=t)
        for i, t in enumerate(draft.acceptance_criteria)
    ]
    spec.open_questions = [
        OpenQuestion(id=f"q{i + 1}", text=t)
        for i, t in enumerate(draft.open_questions)
    ]
    return spec
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/core/test_spec_draft.py -v`
Expected: PASS (Task 3 + Task 4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/spec_draft.py tests/core/test_spec_draft.py
git commit -m "feat(spec): apply_draft merges SpecDraft into Spec (renders body, assigns ids)"
mship journal "apply_draft: render canonical body, set fields, assign ac/q ids" --action committed
```

---

## Task 5: `mship spec draft` command

**Files:** Modify `src/mship/cli/spec.py`; Test `tests/cli/test_spec.py`.

- [ ] **Step 1: Write the failing test** (append to `tests/cli/test_spec.py`)

```python
def test_spec_draft_emits_prompt(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    result = runner.invoke(app, ["spec", "draft", "dq", "--from-text", "rambled intent here"])
    assert result.exit_code == 0, result.output
    assert "rambled intent here" in result.output
    assert "mship spec apply dq --from-json" in result.output


def test_spec_draft_requires_exactly_one_source(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    neither = runner.invoke(app, ["spec", "draft", "dq"])
    assert neither.exit_code != 0
    both = runner.invoke(app, ["spec", "draft", "dq", "--from-text", "x", "--from-file", "f.md"])
    assert both.exit_code != 0


def test_spec_draft_unknown_id_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "draft", "nope", "--from-text", "x"])
    assert result.exit_code != 0
    assert "nope" in result.output
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/cli/test_spec.py -k spec_draft -v`
Expected: FAIL — no `draft` command (`No such command`).

- [ ] **Step 3: Implement** — add inside `register`, after the `new` command:

```python
    @spec_app.command("draft")
    def draft(
        spec_id: str = typer.Argument(..., help="Spec id to draft (must already exist)."),
        from_text: Optional[str] = typer.Option(None, "--from-text", help="Inline intent text."),
        from_file: Optional[str] = typer.Option(None, "--from-file", help="Read intent from a file."),
    ):
        """Emit a drafting prompt for `<id>` to stdout (run it through your agent, then `spec apply`)."""
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME
        from mship.core.spec_draft import build_draft_prompt

        output = Output()
        if (from_text is None) == (from_file is None):
            output.error("Provide exactly one of --from-text or --from-file.")
            raise typer.Exit(1)

        container = get_container()
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)
        if store.find_by_id(spec_id) is None:
            output.error(f"No spec with id {spec_id!r}. Create it first with `mship spec new`.")
            raise typer.Exit(1)

        intent = from_text if from_text is not None else Path(from_file).read_text()
        typer.echo(build_draft_prompt(spec_id, intent))
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/cli/test_spec.py -k spec_draft -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/spec.py tests/cli/test_spec.py
git commit -m "feat(spec): 'mship spec draft' emits a drafting prompt"
mship journal "mship spec draft emits prompt; --from-text xor --from-file; unknown-id errors" --action committed
```

---

## Task 6: `mship spec apply` command

**Files:** Modify `src/mship/cli/spec.py`; Test `tests/cli/test_spec.py`.

- [ ] **Step 1: Write the failing test** (append to `tests/cli/test_spec.py`)

```python
import json as _json


def _draft_json() -> str:
    return _json.dumps({
        "problem": "P", "user_story": "U", "approach": "A",
        "acceptance_criteria": ["view questions"], "open_questions": ["Android?"],
        "non_goals": ["chat"], "risks": [], "affected_repos": ["mothership"],
    })


def test_spec_apply_merges_and_advances_status(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"
    jf.write_text(_draft_json())
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.status == "needs_review"
    assert [c.id for c in spec.acceptance_criteria] == ["ac1"]
    assert "## Problem" in spec.body


def test_spec_apply_rejects_invalid_json(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "bad.json"
    jf.write_text('{"problem": "only problem"}')   # missing required fields
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    assert result.exit_code != 0


def test_spec_apply_refuses_wrong_status(configured_app_with_task: Path, tmp_path):
    # A freshly-applied spec is needs_review; applying again is an illegal transition.
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"; jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])           # -> needs_review
    again = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])    # needs_review -> needs_review illegal
    assert again.exit_code != 0
    forced = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf), "--bypass-status-gate"])
    assert forced.exit_code == 0, forced.output
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/cli/test_spec.py -k spec_apply -v`
Expected: FAIL — no `apply` command.

- [ ] **Step 3: Implement** — add inside `register`, after `draft`:

```python
    @spec_app.command("apply")
    def apply(
        spec_id: str = typer.Argument(..., help="Spec id to apply the draft to."),
        from_json: str = typer.Option(..., "--from-json", help="Path to the draft JSON, or - for stdin."),
        bypass_status_gate: bool = typer.Option(False, "--bypass-status-gate", help="Apply regardless of current status."),
    ):
        """Ingest a SpecDraft JSON: render the body, set fields, advance to needs_review."""
        import json
        import sys
        from datetime import datetime, timezone
        from pathlib import Path
        from pydantic import ValidationError
        from mship.core.spec import SpecDraft, InvalidTransition, validate_transition
        from mship.core.spec_draft import apply_draft
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME

        output = Output()
        raw = sys.stdin.read() if from_json == "-" else Path(from_json).read_text()
        try:
            draft = SpecDraft(**json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            output.error(f"Invalid draft JSON: {e}")
            raise typer.Exit(1)

        container = get_container()
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)

        if not bypass_status_gate:
            try:
                validate_transition(spec.status, "needs_review")
            except InvalidTransition as e:
                output.error(f"{e}. Use --bypass-status-gate to override.")
                raise typer.Exit(1)

        apply_draft(spec, draft)
        spec.status = "needs_review"
        spec.updated_at = datetime.now(timezone.utc)
        path = store.save(spec)

        if output.is_tty:
            output.success(f"Applied draft → {spec.status}: {path}")
        else:
            output.json({"id": spec.id, "status": spec.status, "path": str(path)})
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/cli/test_spec.py -k spec_apply -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/spec.py tests/cli/test_spec.py
git commit -m "feat(spec): 'mship spec apply' ingests a SpecDraft JSON → needs_review"
mship journal "mship spec apply: ingest draft JSON, render body, gate status; --bypass-status-gate" --action committed
```

---

## Task 7: `mship spec validate` command

**Files:** Modify `src/mship/cli/spec.py`; Test `tests/cli/test_spec.py`.

- [ ] **Step 1: Write the failing test** (append to `tests/cli/test_spec.py`)

```python
def test_spec_validate_passes_on_applied_spec(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"; jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    result = runner.invoke(app, ["spec", "validate", "dq"])
    assert result.exit_code == 0, result.output


def test_spec_validate_flags_missing_section(configured_app_with_task: Path):
    # `spec new` writes a template body with all sections, so hand-break one.
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    store = _store(configured_app_with_task)
    spec = store.find_by_id("dq")
    spec.body = "## Problem\n\njust the problem\n"   # drop User story + Approach
    store.save(spec)
    result = runner.invoke(app, ["spec", "validate", "dq"])
    assert result.exit_code != 0
    assert "User story" in result.output or "Approach" in result.output


def test_spec_validate_unknown_id_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "validate", "nope"])
    assert result.exit_code != 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/cli/test_spec.py -k spec_validate -v`
Expected: FAIL — no `validate` command.

- [ ] **Step 3: Implement** — add inside `register`, after `apply`:

```python
    @spec_app.command("validate")
    def validate(
        spec_id: str = typer.Argument(..., help="Spec id to validate."),
    ):
        """Check a spec conforms: frontmatter validates + canonical body sections present."""
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME, SpecParseError, parse_spec
        from mship.core.spec_body import validate_body_structure

        output = Output()
        container = get_container()
        workspace_root = Path(container.config_path()).parent
        specs_dir = workspace_root / SPECS_DIRNAME

        matches = sorted(specs_dir.glob(f"*-{spec_id}.md"))
        if not matches:
            output.error(f"No spec file for id {spec_id!r} in {specs_dir}.")
            raise typer.Exit(1)

        try:
            spec = parse_spec(matches[0].read_text())
        except SpecParseError as e:
            output.error(f"{spec_id}: invalid frontmatter — {e}")
            raise typer.Exit(1)

        missing = validate_body_structure(spec.body)
        if missing:
            output.error(f"{spec_id}: missing body section(s): {', '.join(missing)}")
            raise typer.Exit(1)

        if output.is_tty:
            output.success(f"{spec_id}: valid")
        else:
            output.json({"id": spec_id, "valid": True})
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/cli/test_spec.py -k spec_validate -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `mship test` (records evidence) or `uv run pytest`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/spec.py tests/cli/test_spec.py
git commit -m "feat(spec): 'mship spec validate' enforces the structured-markdown contract"
mship journal "mship spec validate: frontmatter + canonical body sections; full suite green" --action committed
```

---

## Self-Review

**Spec coverage** (design → task):
- Agent boundary (emit/ingest) → Task 3 (`build_draft_prompt`, no model call) + Task 5 (`draft` emits) + Task 6 (`apply` ingests).
- `SpecDraft` model → Task 1.
- Prose → rendered canonical body → Task 2 (`render_body`) + Task 4 (`apply_draft` uses it).
- mship-owned `ac/q` ids → Task 4.
- `→ needs_review` transition + `--bypass-status-gate` → Task 6.
- Structure validation ("simple parser") + `spec validate` → Task 2 + Task 7.
- Input-source `--from-text` xor `--from-file` → Task 5.

**Out of scope (correct):** `spec review` (A3), `questions` (A4), `approve` (A5), `dispatch --spec` (A6), serve API (B1).

**Placeholder scan:** none — every code step is complete.

**Type consistency:** `SpecDraft`, `render_body`/`parse_body_sections`/`validate_body_structure`/`REQUIRED_SECTIONS`, `build_draft_prompt(spec_id, intent_text)`, `apply_draft(spec, draft)`, the three CLI command names, and reuse of A1's `Spec`/`AcceptanceCriterion`/`OpenQuestion`/`validate_transition`/`InvalidTransition`/`SpecStore`/`SPECS_DIRNAME`/`parse_spec`/`SpecParseError` — all consistent.

---

## Execution Handoff

Implement in a dedicated worktree. **Before any code:** commit this plan + the design doc to `main`, then `mship spawn` a task for MOS-146 (real work in the `mothership` repo).
