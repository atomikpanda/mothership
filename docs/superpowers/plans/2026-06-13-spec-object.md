# Structured Spec Object Implementation Plan (MOS-145)

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give mship a first-class, markdown-canonical `Spec` object (frontmatter + body) with a status lifecycle, a registry, a task↔spec pointer, and a migrated `mship spec new` that writes structured specs to `<workspace>/specs/`.

**Architecture:** A `Spec` pydantic model is the in-memory shape; the canonical artifact is a markdown file with YAML frontmatter in `<workspace_root>/specs/YYYY-MM-DD-<id>.md`. `SpecStore` parses/serializes/registers files (no content in `state.yaml`; only a `Task.spec_id` pointer). A central transition map governs `Spec.status`. `find_spec` is extended so `mship view spec` and the dev-gate can resolve specs from `specs/`. Design: [docs/superpowers/specs/2026-06-13-spec-object-design.md](../specs/2026-06-13-spec-object-design.md).

**Tech Stack:** Python, pydantic v2, PyYAML, Typer, pytest. Mirrors the existing `Task`/`StateManager` patterns in `src/mship/core/state.py`.

---

## File structure

- **Create** `src/mship/core/spec.py` — `Spec` model + nested `AcceptanceCriterion`/`OpenQuestion`, `SpecStatus`, the transition map, `validate_transition`. One responsibility: the Spec domain model + its lifecycle rules.
- **Create** `src/mship/core/spec_store.py` — frontmatter `parse_spec`/`serialize_spec` + `SpecStore` (filesystem registry: save/load/list/find_by_id). One responsibility: Spec persistence + discovery.
- **Modify** `src/mship/core/state.py` — add `Task.spec_id: str | None`.
- **Modify** `src/mship/core/view/spec_discovery.py` — extend `find_spec` to resolve `specs/` by id and by `task_slug`.
- **Modify** `src/mship/cli/spec.py` — repurpose `mship spec new` to write structured specs into `specs/`, task-optional.
- **Test** `tests/core/test_spec.py` (create), `tests/core/test_spec_store.py` (create), `tests/core/test_state.py` (extend), `tests/core/view/test_spec_discovery.py` (extend), `tests/cli/test_spec.py` (rewrite the `spec new` cases).

Run all tests with: `uv run pytest` (the repo uses `uv`; `testpaths = ["tests"]`).

---

## Task 1: `Spec` model + nested types

**Files:**
- Create: `src/mship/core/spec.py`
- Test: `tests/core/test_spec.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_spec.py
from datetime import datetime, timezone

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec


def _spec(**kw):
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    base = dict(id="demo", title="Demo", status="drafting", created_at=now, updated_at=now)
    base.update(kw)
    return Spec(**base)


def test_spec_defaults_are_empty():
    s = _spec()
    assert s.affected_repos == []
    assert s.acceptance_criteria == []
    assert s.open_questions == []
    assert s.non_goals == []
    assert s.risks == []
    assert s.task_slug is None
    assert s.body == ""


def test_dispatch_ready_requires_approved_and_no_open_questions():
    s = _spec(status="approved", open_questions=[OpenQuestion(id="q1", text="?", answer=None)])
    assert s.dispatch_ready is False
    s2 = _spec(status="approved", open_questions=[OpenQuestion(id="q1", text="?", answer="yes")])
    assert s2.dispatch_ready is True
    s3 = _spec(status="needs_review")
    assert s3.dispatch_ready is False


def test_acceptance_criterion_verdict_defaults_unreviewed():
    ac = AcceptanceCriterion(id="ac1", text="works")
    assert ac.verdict == "unreviewed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_spec.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.spec'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mship/core/spec.py
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


SpecStatus = Literal[
    "captured", "drafting", "needs_review", "needs_clarification",
    "approved", "dispatched", "implemented", "archived",
]


class AcceptanceCriterion(BaseModel):
    id: str
    text: str
    verdict: Literal["unreviewed", "approved", "flagged"] = "unreviewed"


class OpenQuestion(BaseModel):
    id: str
    text: str
    answer: str | None = None


class Spec(BaseModel):
    id: str
    title: str
    status: SpecStatus
    created_at: datetime
    updated_at: datetime
    affected_repos: list[str] = []
    acceptance_criteria: list[AcceptanceCriterion] = []
    open_questions: list[OpenQuestion] = []
    non_goals: list[str] = []
    risks: list[str] = []
    task_slug: str | None = None
    body: str = ""

    @property
    def dispatch_ready(self) -> bool:
        return self.status == "approved" and all(
            q.answer is not None for q in self.open_questions
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_spec.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit (pair with `mship journal` in a mothership workspace)**

```bash
git add src/mship/core/spec.py tests/core/test_spec.py
git commit -m "feat(spec): add Spec model + nested acceptance/question types"
mship journal "added Spec pydantic model + dispatch_ready; unit tests passing" --action committed
```

---

## Task 2: Status transition map + validator

**Files:**
- Modify: `src/mship/core/spec.py`
- Test: `tests/core/test_spec.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_spec.py (append)
import pytest

from mship.core.spec import InvalidTransition, can_transition, validate_transition


@pytest.mark.parametrize("current,target", [
    ("captured", "drafting"),
    ("drafting", "needs_review"),
    ("needs_review", "approved"),
    ("needs_review", "needs_clarification"),
    ("needs_clarification", "needs_review"),
    ("approved", "dispatched"),
    ("approved", "needs_clarification"),   # re-open
    ("dispatched", "implemented"),
    ("implemented", "archived"),
    ("drafting", "archived"),              # abandon from any non-terminal
    ("approved", "archived"),              # abandon
])
def test_legal_transitions_allowed(current, target):
    assert can_transition(current, target) is True
    validate_transition(current, target)  # must not raise


@pytest.mark.parametrize("current,target", [
    ("captured", "approved"),     # skips drafting/review
    ("drafting", "dispatched"),   # skips review/approval
    ("archived", "drafting"),     # terminal
    ("approved", "approved"),     # no-op
])
def test_illegal_transitions_rejected(current, target):
    assert can_transition(current, target) is False
    with pytest.raises(InvalidTransition):
        validate_transition(current, target)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_spec.py -k transition -v`
Expected: FAIL — `ImportError: cannot import name 'InvalidTransition'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mship/core/spec.py (append)
TERMINAL_STATUSES: set[str] = {"archived"}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "captured": {"drafting"},
    "drafting": {"needs_review"},
    "needs_review": {"needs_clarification", "approved"},
    "needs_clarification": {"needs_review", "drafting"},
    "approved": {"dispatched", "needs_clarification"},
    "dispatched": {"implemented"},
    "implemented": {"archived"},
    "archived": set(),
}


class InvalidTransition(Exception):
    pass


def can_transition(current: str, target: str) -> bool:
    if current == target:
        return False
    # Abandon: any non-terminal status may jump to archived.
    if target == "archived" and current not in TERMINAL_STATUSES:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, set())


def validate_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(f"illegal spec transition: {current} -> {target}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_spec.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/spec.py tests/core/test_spec.py
git commit -m "feat(spec): add status transition map + validate_transition"
mship journal "added spec lifecycle transition validator (incl. abandon edge); tests passing" --action committed
```

---

## Task 3: Frontmatter parse / serialize

**Files:**
- Create: `src/mship/core/spec_store.py`
- Test: `tests/core/test_spec_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_spec_store.py
from datetime import datetime, timezone

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_store import SpecParseError, parse_spec, serialize_spec


def _spec():
    now = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
    return Spec(
        id="decision-queue", title="Decision queue", status="needs_review",
        created_at=now, updated_at=now,
        affected_repos=["mothership", "ground-control"],
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="view questions")],
        open_questions=[OpenQuestion(id="q1", text="Android in v0?")],
        non_goals=["chat"],
        body="## Problem\n\nAgents block away from the desk.\n",
    )


def test_round_trip_is_identity():
    s = _spec()
    assert parse_spec(serialize_spec(s)) == s


def test_body_is_preserved_verbatim():
    s = _spec()
    parsed = parse_spec(serialize_spec(s))
    assert parsed.body == "## Problem\n\nAgents block away from the desk.\n"


def test_missing_frontmatter_raises():
    import pytest
    with pytest.raises(SpecParseError):
        parse_spec("# just markdown, no frontmatter\n")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_spec_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.spec_store'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mship/core/spec_store.py
from __future__ import annotations

import yaml

from mship.core.spec import Spec


class SpecParseError(Exception):
    pass


def parse_spec(text: str) -> Spec:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SpecParseError("spec file missing YAML frontmatter")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise SpecParseError("unterminated YAML frontmatter")
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1:])
    data = yaml.safe_load(fm_text) or {}
    return Spec(**data, body=body)


def serialize_spec(spec: Spec) -> str:
    data = spec.model_dump(mode="json", exclude={"body"})
    fm = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    return f"---\n{fm}---\n{spec.body}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_spec_store.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/spec_store.py tests/core/test_spec_store.py
git commit -m "feat(spec): markdown frontmatter parse/serialize with round-trip fidelity"
mship journal "spec frontmatter parse/serialize round-trips; body preserved verbatim" --action committed
```

---

## Task 4: `SpecStore` registry (save / load / list / find_by_id)

**Files:**
- Modify: `src/mship/core/spec_store.py`
- Test: `tests/core/test_spec_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_spec_store.py (append)
from pathlib import Path

from mship.core.spec_store import SpecStore


def _new_spec(spec_id: str):
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    return Spec(id=spec_id, title=spec_id, status="drafting", created_at=now, updated_at=now)


def test_save_then_find_by_id(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    path = store.save(_new_spec("alpha"))
    assert path.name == "2026-06-13-alpha.md"
    assert path.is_file()
    found = store.find_by_id("alpha")
    assert found is not None and found.id == "alpha"


def test_find_by_id_is_exact_not_mtime(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    store.save(_new_spec("alpha"))
    store.save(_new_spec("beta"))   # newer mtime
    # Exact id match, not newest-by-mtime (subsumes MOS-140).
    assert store.find_by_id("alpha").id == "alpha"


def test_list_returns_all(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    store.save(_new_spec("alpha"))
    store.save(_new_spec("beta"))
    assert sorted(s.id for s in store.list()) == ["alpha", "beta"]


def test_find_by_id_missing_returns_none(tmp_path: Path):
    assert SpecStore(tmp_path / "specs").find_by_id("nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_spec_store.py -k "save or find or list" -v`
Expected: FAIL — `ImportError: cannot import name 'SpecStore'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mship/core/spec_store.py (append)
import tempfile
from pathlib import Path


class SpecStore:
    """Filesystem registry for markdown-canonical specs under `specs/`."""

    def __init__(self, specs_dir: Path) -> None:
        self._dir = Path(specs_dir)

    def path_for(self, spec: Spec) -> Path:
        return self._dir / f"{spec.created_at:%Y-%m-%d}-{spec.id}.md"

    def save(self, spec: Spec) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(spec)
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".md.tmp")
        try:
            with open(fd, "w") as f:
                f.write(serialize_spec(spec))
            Path(tmp).replace(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def load(self, path: Path) -> Spec:
        return parse_spec(Path(path).read_text())

    def list(self) -> list[Spec]:
        if not self._dir.is_dir():
            return []
        return [self.load(p) for p in sorted(self._dir.glob("*.md"))]

    def find_by_id(self, spec_id: str) -> Spec | None:
        for spec in self.list():
            if spec.id == spec_id:
                return spec
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_spec_store.py -v`
Expected: PASS (all Task 3 + Task 4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/spec_store.py tests/core/test_spec_store.py
git commit -m "feat(spec): SpecStore registry with atomic save + exact id lookup"
mship journal "SpecStore save/list/find_by_id; id match is exact (subsumes #140 for specs/)" --action committed
```

---

## Task 5: `Task.spec_id` pointer

**Files:**
- Modify: `src/mship/core/state.py:19-37` (the `Task` model)
- Test: `tests/core/test_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_state.py (append)
def test_task_spec_id_defaults_none():
    from datetime import datetime, timezone
    from mship.core.state import Task
    t = Task(
        slug="t", description="d", phase="plan",
        created_at=datetime.now(timezone.utc),
        affected_repos=["a"], branch="feat/t",
    )
    assert t.spec_id is None


def test_task_spec_id_round_trips(tmp_path):
    from datetime import datetime, timezone
    from mship.core.state import StateManager, Task, WorkspaceState
    sm = StateManager(tmp_path)
    sm.save(WorkspaceState(tasks={"t": Task(
        slug="t", description="d", phase="plan",
        created_at=datetime.now(timezone.utc),
        affected_repos=["a"], branch="feat/t", spec_id="decision-queue",
    )}))
    assert sm.load().tasks["t"].spec_id == "decision-queue"


def test_legacy_state_without_spec_id_loads(tmp_path):
    """Old state.yaml (no spec_id key) loads cleanly (default None)."""
    import yaml
    from mship.core.state import StateManager
    (tmp_path / "state.yaml").write_text(yaml.safe_dump({
        "tasks": {"t": {
            "slug": "t", "description": "d", "phase": "dev",
            "created_at": "2026-04-10T00:00:00+00:00",
            "affected_repos": ["a"], "branch": "feat/t",
        }}
    }))
    assert StateManager(tmp_path).load().tasks["t"].spec_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_state.py -k spec_id -v`
Expected: FAIL — `AttributeError: 'Task' object has no attribute 'spec_id'`.

- [ ] **Step 3: Write minimal implementation**

In `src/mship/core/state.py`, add one field to `Task` (after `passive_repos`):

```python
    passive_repos: set[str] = set()
    spec_id: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_state.py -v`
Expected: PASS (existing + 3 new tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/state.py tests/core/test_state.py
git commit -m "feat(spec): add Task.spec_id pointer (back-compat default None)"
mship journal "Task.spec_id pointer added; legacy state.yaml loads cleanly" --action committed
```

---

## Task 6: Extend `find_spec` to resolve `specs/`

**Files:**
- Modify: `src/mship/core/view/spec_discovery.py:24-71` (the `find_spec` function) + add a helper
- Test: `tests/core/view/test_spec_discovery.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/core/view/test_spec_discovery.py (append)
from datetime import datetime, timezone
from pathlib import Path

from mship.core.spec import Spec
from mship.core.spec_store import SpecStore
from mship.core.view.spec_discovery import find_spec


def _seed(tmp_path: Path, spec_id: str, task_slug=None) -> Path:
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    store = SpecStore(tmp_path / "specs")
    return store.save(Spec(
        id=spec_id, title=spec_id, status="drafting",
        created_at=now, updated_at=now, task_slug=task_slug,
    ))


def test_find_spec_resolves_specs_dir_by_id(tmp_path: Path):
    path = _seed(tmp_path, "decision-queue")
    assert find_spec(tmp_path, "decision-queue") == path


def test_find_spec_resolves_specs_dir_by_task_slug(tmp_path: Path):
    from mship.core.state import Task, WorkspaceState
    path = _seed(tmp_path, "decision-queue", task_slug="dq")
    state = WorkspaceState(tasks={"dq": Task(
        slug="dq", description="d", phase="plan",
        created_at=datetime(2026, 6, 13, tzinfo=timezone.utc),
        affected_repos=["a"], branch="feat/dq",
    )})
    assert find_spec(tmp_path, None, task="dq", state=state) == path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/view/test_spec_discovery.py -k specs_dir -v`
Expected: FAIL — `SpecNotFoundError` (specs/ not consulted yet).

- [ ] **Step 3: Write minimal implementation**

Add the helper + constant near the top of `src/mship/core/view/spec_discovery.py`:

```python
SPECS_DIR = Path("specs")


def _find_in_specs_dir(workspace_root: Path, *, spec_id=None, task_slug=None):
    """Return the path of a spec file in `<workspace_root>/specs` matching
    `spec_id` (frontmatter id) or `task_slug` (bound task), else None."""
    from mship.core.spec_store import SpecParseError, parse_spec
    specs_dir = workspace_root / SPECS_DIR
    if not specs_dir.is_dir():
        return None
    for p in sorted(specs_dir.glob("*.md")):
        try:
            spec = parse_spec(p.read_text())
        except SpecParseError:
            continue
        if spec_id is not None and spec.id == spec_id:
            return p
        if task_slug is not None and spec.task_slug == task_slug:
            return p
    return None
```

Then wire it into `find_spec`. After the blessed-path block (the `if task is not None and name_or_path is None:` clause) add a `specs/` lookup, and add an id lookup for the named case. The function becomes:

```python
def find_spec(workspace_root, name_or_path, *, task=None, state=None, spec_paths=None):
    if name_or_path is not None:
        candidate = Path(name_or_path)
        if candidate.is_absolute():
            if candidate.is_file():
                return candidate
            raise SpecNotFoundError(f"Spec not found: {name_or_path}")

    if task is not None and name_or_path is None:
        blessed = blessed_spec_path(workspace_root, task)
        if blessed.is_file():
            return blessed
        in_specs = _find_in_specs_dir(workspace_root, task_slug=task)
        if in_specs is not None:
            return in_specs

    if name_or_path is not None:
        by_id = _find_in_specs_dir(workspace_root, spec_id=name_or_path)
        if by_id is not None:
            return by_id

    search_roots = _resolve_search_roots(workspace_root, task, state, spec_paths)

    if name_or_path is None:
        return _newest_across(search_roots, task)

    for root in search_roots:
        for candidate_name in (name_or_path, f"{name_or_path}.md"):
            p = root / candidate_name
            if p.is_file():
                return p

    available_msg = _available_msg(search_roots)
    where = f"task {task!r}" if task else "any known location"
    raise SpecNotFoundError(f"Spec not found: {name_or_path!r} (searched {where}).{available_msg}")
```

(The blessed-path precedence and all legacy fallbacks are unchanged; `specs/` is consulted *after* the blessed path and *before* the newest-by-mtime fallback.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/view/test_spec_discovery.py -v`
Expected: PASS (existing discovery tests + 2 new). The pre-existing blessed-path test still passes (precedence preserved).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/view/spec_discovery.py tests/core/view/test_spec_discovery.py
git commit -m "feat(spec): find_spec resolves specs/ by id and task_slug"
mship journal "find_spec now discovers specs/ (by id + task binding); blessed precedence intact" --action committed
```

---

## Task 7: Migrate `mship spec new` to structured specs in `specs/`

**Files:**
- Modify: `src/mship/cli/spec.py` (replace `SPEC_TEMPLATE`/`render_template`/`new`; remove now-dead helpers)
- Test: `tests/cli/test_spec.py` (rewrite the `spec new` cases)

- [ ] **Step 1: Confirm no external importers of the dead helpers**

Run: `rg -n "render_template|SPEC_TEMPLATE|from mship.cli.spec import" src tests`
Expected: only references inside `src/mship/cli/spec.py` itself (safe to remove). If anything else imports them, leave a thin shim — do not break it.

- [ ] **Step 2: Rewrite the failing tests**

Replace the five `test_spec_new_*` tests (the blessed-path ones) in `tests/cli/test_spec.py` with the new contract. Keep the `find_spec`/gate tests (`test_find_spec_discovers_blessed_path_when_task_set`, `test_gate_dev_*`) untouched — blessed-path behavior is still valid.

```python
# tests/cli/test_spec.py — replace the test_spec_new_* block with:
from mship.core.spec_store import SpecStore


def _store(workspace: Path) -> SpecStore:
    return SpecStore(workspace / "specs")


def test_spec_new_creates_structured_file(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--title", "Add labels"])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("add-labels")
    assert spec is not None
    assert spec.status == "drafting"
    assert spec.title == "Add labels"
    assert "## Problem" in spec.body


def test_spec_new_with_task_prefills_repos_and_binds(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("add-labels")
    assert spec is not None
    assert spec.task_slug == "add-labels"
    assert spec.affected_repos == ["shared", "auth-service"]
    assert spec.title == "Add labels to tasks"


def test_spec_new_requires_title_or_task(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new"])
    assert result.exit_code != 0
    assert "title" in result.output.lower()


def test_spec_new_refuses_existing(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Add labels"])
    result = runner.invoke(app, ["spec", "new", "--title", "Add labels"])
    assert result.exit_code != 0
    assert "exists" in result.output.lower() or "already" in result.output.lower()


def test_spec_new_force_overwrites(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Add labels"])
    result = runner.invoke(app, ["spec", "new", "--title", "Add labels", "--force"])
    assert result.exit_code == 0, result.output


def test_spec_new_unknown_task_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--task", "nope"])
    assert result.exit_code != 0
    assert "nope" in result.output
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_spec.py -k spec_new -v`
Expected: FAIL — old `new` writes the blessed path / requires `--task`; new assertions about `specs/` + `--title` don't hold yet.

- [ ] **Step 4: Implement the migrated command**

Replace the body of `src/mship/cli/spec.py` from `SPEC_TEMPLATE` through the `new` command with:

```python
SPEC_BODY_TEMPLATE = """\
## Problem

_What problem does this solve? Why now?_

## User story

_As a <user>, I want <capability>, so that <benefit>._

## Approach

_How will it work? Key decisions._
"""


def register(parent: typer.Typer, get_container):
    spec_app = typer.Typer(
        name="spec",
        help="Manage structured specs (`<workspace>/specs/<date>-<id>.md`).",
        no_args_is_help=True,
    )

    @spec_app.command("new")
    def new(
        title: Optional[str] = typer.Option(None, "--title", help="Spec title (required unless --task is given)."),
        spec_id: Optional[str] = typer.Option(None, "--id", help="Stable spec id (slug). Defaults to a slug of the title."),
        task_opt: Optional[str] = typer.Option(None, "--task", help="Bind to an existing task: prefill affected_repos + task_slug."),
        force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing spec file."),
    ):
        """Create a structured spec at `<workspace>/specs/YYYY-MM-DD-<id>.md`."""
        from mship.core.spec import Spec
        from mship.core.spec_store import SpecStore
        from mship.util.slug import slugify

        container = get_container()
        output = Output()
        workspace_root = Path(container.config_path()).parent
        store = SpecStore(workspace_root / "specs")

        affected_repos: list[str] = []
        task_slug: Optional[str] = None
        if task_opt is not None:
            state = container.state_manager().load()
            if task_opt not in state.tasks:
                known = ", ".join(sorted(state.tasks)) or "(none)"
                output.error(f"Unknown task: {task_opt}. Known: {known}.")
                raise typer.Exit(1)
            t = state.tasks[task_opt]
            affected_repos = list(t.affected_repos)
            task_slug = t.slug
            if title is None:
                title = t.description
            if spec_id is None:
                spec_id = t.slug

        if title is None:
            output.error("Provide --title (or --task to derive it).")
            raise typer.Exit(1)
        if spec_id is None:
            spec_id = slugify(title)

        now = datetime.now(timezone.utc)
        spec = Spec(
            id=spec_id, title=title, status="drafting",
            created_at=now, updated_at=now,
            affected_repos=affected_repos, task_slug=task_slug,
            body=SPEC_BODY_TEMPLATE,
        )
        path = store.path_for(spec)
        if path.exists() and not force:
            output.error(f"Spec already exists: {path}\n  Pass --force to overwrite.")
            raise typer.Exit(1)
        store.save(spec)

        if output.is_tty:
            output.success(f"Created spec: {path}")
            output.print("[dim]Edit the prose; lifecycle commands (draft/review/approve) follow.[/dim]")
        else:
            output.json({
                "id": spec.id, "path": str(path),
                "status": spec.status, "task_slug": task_slug,
            })

    parent.add_typer(spec_app)
```

Delete the now-unused `SPEC_TEMPLATE`, `render_template`, and `blessed_spec_path` from `src/mship/cli/spec.py`, plus the `resolve_for_command` import if Step 1 confirmed nothing else uses them. (The `blessed_spec_path` used by discovery lives in `spec_discovery.py` and is untouched.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_spec.py -v`
Expected: PASS — new `spec_new` tests pass; the retained `find_spec`/gate tests still pass.

- [ ] **Step 6: Run the full suite (no regressions)**

Run: `uv run pytest`
Expected: PASS. If any unrelated test imported the deleted helpers, fix per Step 1.

- [ ] **Step 7: Commit**

```bash
git add src/mship/cli/spec.py tests/cli/test_spec.py
git commit -m "feat(spec): migrate 'mship spec new' to structured specs/ (task-optional)"
mship journal "mship spec new writes structured frontmatter specs to specs/; task-optional" --action committed
```

---

## Self-Review

**Spec coverage** (design doc → task):
- Data model / frontmatter schema → Task 1 (model), Task 3 (frontmatter).
- Status lifecycle + transition map → Task 2.
- Relationship to Task (`spec_id`/`task_slug`) → Task 5 (`Task.spec_id`); `task_slug` is on `Spec` (Task 1) and set by `spec new --task` (Task 7).
- Discovery / registry → Task 4 (`SpecStore`), Task 6 (`find_spec`).
- `mship spec new` migration (task-optional) → Task 7.
- Migration/back-compat → Task 5 (legacy state.yaml), Task 6 (blessed + legacy paths retained).
- Testing (round-trip, transition, registry, back-compat) → Tasks 1–7.

**Out of scope (correct — separate issues):** `spec draft` agent boundary (A2), review-unit emission (A3), questions (A4), approve (A5), dispatch wiring + the `approved→dispatched`/`dispatched→implemented` boundary writes (A6), serve API (B1), dev-gate *requiring* an approved spec (A7).

**Placeholder scan:** none — every code step shows complete code; prose-template underscores are intentional file content.

**Type consistency:** `Spec`, `AcceptanceCriterion`, `OpenQuestion`, `SpecStatus`, `InvalidTransition`, `can_transition`/`validate_transition`, `parse_spec`/`serialize_spec`, `SpecStore.{path_for,save,load,list,find_by_id}`, `Task.spec_id` — names are consistent across tasks.

---

## Execution Handoff

Implement in a dedicated worktree. **First step before any code:** `mship spawn` a task for MOS-145 (this is real work in the `mothership` repo, and we're on `main`).
