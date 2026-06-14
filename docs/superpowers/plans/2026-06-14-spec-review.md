# `mship spec review` / `verdict` Implementation Plan (MOS-147)

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface a spec's review units (acceptance criteria + open questions + read-only context) for the Ground Control review cards, and let a reviewer record a per-criterion verdict — no model calls, no status transitions.

**Architecture:** A thin `core/spec_review.py` builds a strictly-factual review payload from a `Spec` (reusing A2's `parse_body_sections`) and records a single criterion verdict. Two CLI commands wrap it. Verdicts live on the existing `AcceptanceCriterion.verdict` (A1) — no model change. Design: [docs/superpowers/specs/2026-06-14-spec-review-design.md](../specs/2026-06-14-spec-review-design.md).

**Tech Stack:** Python, pydantic v2, Typer, pytest. Builds on `core/spec.py`, `core/spec_body.py`, `core/spec_store.py`, `cli/spec.py` (A1+A2, in `main`).

---

## File structure

- **Create** `src/mship/core/spec_review.py` — `VERDICTS`, `build_review(spec) -> dict`, `set_criterion_verdict(spec, criterion_id, verdict) -> Spec`. One responsibility: the review payload + verdict mutation.
- **Modify** `src/mship/cli/spec.py` — add `review` + `verdict` commands.
- **Test** `tests/core/test_spec_review.py` (create), `tests/cli/test_spec.py` (extend).

Output follows the existing TTY-detection pattern (non-TTY → JSON, like `new`/`apply`); no explicit `--json` flag (MOS-103 will standardize flags globally later).

---

## Task 1: `build_review` core helper

**Files:** Create `src/mship/core/spec_review.py`; Test `tests/core/test_spec_review.py`.

- [ ] **Step 1: Write the failing test** (`tests/core/test_spec_review.py`)

```python
from datetime import datetime, timezone

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_review import build_review


def _spec():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    return Spec(
        id="dq", title="DQ", status="needs_review", created_at=now, updated_at=now,
        body=render_body("the problem", "as a user", "the approach"),
        non_goals=["chat"], risks=["scope"], affected_repos=["mothership"],
        acceptance_criteria=[
            AcceptanceCriterion(id="ac1", text="view questions", verdict="approved"),
            AcceptanceCriterion(id="ac2", text="record answer"),  # unreviewed
        ],
        open_questions=[OpenQuestion(id="q1", text="Android?")],
    )


def test_build_review_shapes_units_and_context():
    r = build_review(_spec())
    assert r["id"] == "dq" and r["status"] == "needs_review"
    assert r["acceptance_criteria"] == [
        {"id": "ac1", "text": "view questions", "verdict": "approved"},
        {"id": "ac2", "text": "record answer", "verdict": "unreviewed"},
    ]
    assert r["open_questions"] == [{"id": "q1", "text": "Android?", "answer": None}]
    assert r["context"]["problem"] == "the problem"
    assert r["context"]["approach"] == "the approach"
    assert r["context"]["non_goals"] == ["chat"]


def test_build_review_summary_counts():
    s = build_review(_spec())["summary"]
    assert s == {
        "criteria_total": 2, "approved": 1, "flagged": 0, "unreviewed": 1,
        "open_questions_unanswered": 1,
    }
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/core/test_spec_review.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.spec_review'`.

- [ ] **Step 3: Implement** (`src/mship/core/spec_review.py`)

```python
from __future__ import annotations

from mship.core.spec import Spec
from mship.core.spec_body import parse_body_sections

VERDICTS: tuple[str, ...] = ("unreviewed", "approved", "flagged")


def build_review(spec: Spec) -> dict:
    """Strictly-factual review payload for the Ground Control review cards.

    Quotes the spec verbatim — no inference. Prose context is read-only;
    only acceptance criteria carry verdicts (see design A3)."""
    sections = parse_body_sections(spec.body)
    counts = {v: 0 for v in VERDICTS}
    for c in spec.acceptance_criteria:
        counts[c.verdict] = counts.get(c.verdict, 0) + 1
    return {
        "id": spec.id,
        "status": spec.status,
        "acceptance_criteria": [
            {"id": c.id, "text": c.text, "verdict": c.verdict}
            for c in spec.acceptance_criteria
        ],
        "open_questions": [
            {"id": q.id, "text": q.text, "answer": q.answer}
            for q in spec.open_questions
        ],
        "context": {
            "problem": sections.get("Problem", ""),
            "user_story": sections.get("User story", ""),
            "approach": sections.get("Approach", ""),
            "non_goals": list(spec.non_goals),
            "risks": list(spec.risks),
            "affected_repos": list(spec.affected_repos),
        },
        "summary": {
            "criteria_total": len(spec.acceptance_criteria),
            "approved": counts["approved"],
            "flagged": counts["flagged"],
            "unreviewed": counts["unreviewed"],
            "open_questions_unanswered": sum(
                1 for q in spec.open_questions if q.answer is None
            ),
        },
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/core/test_spec_review.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/spec_review.py tests/core/test_spec_review.py
git commit -m "feat(spec): build_review payload (units + context + summary)"
mship journal "spec_review.build_review emits criteria/questions/context/summary; tests passing" --action committed
```

---

## Task 2: `set_criterion_verdict` core helper

**Files:** Modify `src/mship/core/spec_review.py`; Test `tests/core/test_spec_review.py`.

- [ ] **Step 1: Write the failing test** (append)

```python
import pytest

from mship.core.spec_review import set_criterion_verdict


def test_set_criterion_verdict_updates():
    spec = _spec()
    set_criterion_verdict(spec, "ac2", "flagged")
    assert spec.acceptance_criteria[1].verdict == "flagged"


def test_set_criterion_verdict_rejects_bad_verdict():
    with pytest.raises(ValueError):
        set_criterion_verdict(_spec(), "ac1", "bogus")


def test_set_criterion_verdict_rejects_unknown_id():
    with pytest.raises(ValueError):
        set_criterion_verdict(_spec(), "nope", "approved")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/core/test_spec_review.py -k set_criterion -v`
Expected: FAIL — `ImportError: cannot import name 'set_criterion_verdict'`.

- [ ] **Step 3: Implement** (append to `src/mship/core/spec_review.py`)

```python
def set_criterion_verdict(spec: Spec, criterion_id: str, verdict: str) -> Spec:
    """Set one acceptance criterion's verdict in place. Raises ValueError on an
    invalid verdict or unknown criterion id. Does not change status or persist."""
    if verdict not in VERDICTS:
        raise ValueError(
            f"invalid verdict {verdict!r}; expected one of {', '.join(VERDICTS)}"
        )
    for c in spec.acceptance_criteria:
        if c.id == criterion_id:
            c.verdict = verdict
            return spec
    valid = ", ".join(c.id for c in spec.acceptance_criteria) or "(none)"
    raise ValueError(f"no acceptance criterion {criterion_id!r}; valid ids: {valid}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/core/test_spec_review.py -v`
Expected: PASS (Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/spec_review.py tests/core/test_spec_review.py
git commit -m "feat(spec): set_criterion_verdict with validation"
mship journal "set_criterion_verdict: validates verdict + criterion id; tests passing" --action committed
```

---

## Task 3: `mship spec review` command

**Files:** Modify `src/mship/cli/spec.py`; Test `tests/cli/test_spec.py`.

- [ ] **Step 1: Write the failing test** (append to `tests/cli/test_spec.py`)

```python
def test_spec_review_emits_units(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"; jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    result = runner.invoke(app, ["spec", "review", "dq"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["id"] == "dq"
    assert payload["acceptance_criteria"][0]["id"] == "ac1"
    assert payload["summary"]["criteria_total"] == 1


def test_spec_review_unknown_id_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "review", "nope"])
    assert result.exit_code != 0
    assert "nope" in result.output
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/cli/test_spec.py -k spec_review -v`
Expected: FAIL — no `review` command.

- [ ] **Step 3: Implement** — add inside `register`, after the `validate` command:

```python
    @spec_app.command("review")
    def review(
        spec_id: str = typer.Argument(..., help="Spec id to review."),
    ):
        """Emit a spec's review units (criteria + questions + read-only context)."""
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME
        from mship.core.spec_review import build_review

        output = Output()
        container = get_container()
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)

        payload = build_review(spec)
        if output.is_tty:
            output.print(f"[bold]{payload['id']}[/bold] ({payload['status']})")
            for c in payload["acceptance_criteria"]:
                output.print(f"  [{c['verdict']}] {c['id']}: {c['text']}")
            s = payload["summary"]
            output.print(
                f"  summary: {s['approved']} approved, {s['flagged']} flagged, "
                f"{s['unreviewed']} unreviewed; {s['open_questions_unanswered']} open question(s)"
            )
        else:
            output.json(payload)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/cli/test_spec.py -k spec_review -v`
Expected: PASS (2 tests). Also run the whole file for no regression.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/spec.py tests/cli/test_spec.py
git commit -m "feat(spec): 'mship spec review' emits review units"
mship journal "mship spec review emits review payload (TTY readable / non-TTY JSON)" --action committed
```

---

## Task 4: `mship spec verdict` command

**Files:** Modify `src/mship/cli/spec.py`; Test `tests/cli/test_spec.py`.

- [ ] **Step 1: Write the failing test** (append to `tests/cli/test_spec.py`)

```python
def _apply_dq(tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"; jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])


def test_spec_verdict_sets_and_persists(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "verdict", "dq", "ac1", "approved"])
    assert result.exit_code == 0, result.output
    review = _json.loads(runner.invoke(app, ["spec", "review", "dq"]).output)
    assert review["acceptance_criteria"][0]["verdict"] == "approved"


def test_spec_verdict_rejects_bad_verdict(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "verdict", "dq", "ac1", "bogus"])
    assert result.exit_code != 0
    assert "bogus" in result.output


def test_spec_verdict_rejects_unknown_criterion(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "verdict", "dq", "ac99", "approved"])
    assert result.exit_code != 0
    assert "ac99" in result.output
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/cli/test_spec.py -k spec_verdict -v`
Expected: FAIL — no `verdict` command.

- [ ] **Step 3: Implement** — add inside `register`, after the `review` command:

```python
    @spec_app.command("verdict")
    def verdict(
        spec_id: str = typer.Argument(..., help="Spec id."),
        criterion_id: str = typer.Argument(..., help="Acceptance criterion id (e.g. ac1)."),
        verdict_value: str = typer.Argument(..., metavar="VERDICT", help="unreviewed | approved | flagged."),
    ):
        """Record a verdict on one acceptance criterion (no status change)."""
        from datetime import datetime, timezone
        from pathlib import Path
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME
        from mship.core.spec_review import set_criterion_verdict

        output = Output()
        container = get_container()
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / SPECS_DIRNAME)
        spec = store.find_by_id(spec_id)
        if spec is None:
            output.error(f"No spec with id {spec_id!r}.")
            raise typer.Exit(1)

        try:
            set_criterion_verdict(spec, criterion_id, verdict_value)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(1)

        spec.updated_at = datetime.now(timezone.utc)
        path = store.save(spec)
        if output.is_tty:
            output.success(f"{criterion_id} → {verdict_value}: {path}")
        else:
            output.json({"id": spec.id, "criterion": criterion_id, "verdict": verdict_value})
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/cli/test_spec.py -k spec_verdict -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the FULL suite (regressions)**

Run: `mship test` (records evidence) or `uv run pytest`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/spec.py tests/cli/test_spec.py
git commit -m "feat(spec): 'mship spec verdict' records a criterion verdict"
mship journal "mship spec verdict records criterion verdict; full suite green" --action committed
```

---

## Self-Review

**Spec coverage** (design → task):
- `spec review` emit (criteria + questions + context + summary) → Task 1 (`build_review`) + Task 3 (CLI).
- `spec verdict` record (validate verdict + criterion id) → Task 2 (`set_criterion_verdict`) + Task 4 (CLI).
- No status transition; ungated → Task 4 (only sets verdict + updated_at).
- Strictly factual / prose read-only → Task 1 (context quotes verbatim, no verdicts on prose).

**Out of scope (correct):** `spec questions` (A4), `spec approve` (A5), `dispatch --spec` (A6), per-prose-card verdicts, serve API (B1).

**Placeholder scan:** none — every code step is complete.

**Type consistency:** `VERDICTS`, `build_review(spec) -> dict`, `set_criterion_verdict(spec, criterion_id, verdict) -> Spec`, the two CLI command names; reuse of A1/A2 `Spec`/`AcceptanceCriterion`/`OpenQuestion`/`parse_body_sections`/`SpecStore`/`SPECS_DIRNAME`. The CLI `verdict` arg is named `verdict_value` (metavar `VERDICT`) to avoid shadowing. Consistent.

---

## Execution Handoff

Implement in a dedicated worktree. **Before any code:** commit this plan + the design doc to `main`, then `mship spawn` a task for MOS-147.
