"""Tests for `mship spec new` (#126, #145)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


@pytest.fixture
def configured_app_with_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels to tasks",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))

    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()


@pytest.fixture
def configured_app_git_no_task(workspace_with_git: Path):
    """Git-backed workspace with no tasks — for exercising real auto-spawn."""
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)
    StateManager(state_dir).save(WorkspaceState(tasks={}))
    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()


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


# --- find_spec discovery of the blessed path (#126) ---


def test_find_spec_discovers_blessed_path_when_task_set(tmp_path: Path):
    """`mship view spec` (find_spec with task=<slug>) finds the blessed file."""
    from mship.core.state import Task, WorkspaceState
    from mship.core.view.spec_discovery import find_spec

    blessed = tmp_path / ".mothership" / "tasks" / "demo" / "SPEC.md"
    blessed.parent.mkdir(parents=True)
    blessed.write_text("# demo spec\n")

    task = Task(
        slug="demo",
        description="d",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["a"],
        branch="feat/demo",
    )
    state = WorkspaceState(tasks={"demo": task})
    found = find_spec(tmp_path, None, task="demo", state=state)
    assert found == blessed


# --- _gate_dev satisfaction by blessed path (#126) ---


def test_gate_dev_satisfied_by_blessed_path(tmp_path: Path):
    """`mship phase dev` doesn't warn when the task's blessed SPEC.md exists,
    even with no spec in the workspace-level docs/superpowers/specs dir."""
    from unittest.mock import MagicMock
    from mship.core.config import RepoConfig, WorkspaceConfig
    from mship.core.log import LogManager
    from mship.core.phase import PhaseManager
    from mship.core.state import StateManager, Task, WorkspaceState

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
        worktrees={"shared": tmp_path / "shared"},
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))

    # Place the blessed spec; nothing in docs/superpowers/specs.
    blessed = state_dir / "tasks" / "add-labels" / "SPEC.md"
    blessed.parent.mkdir(parents=True)
    blessed.write_text("# spec\n")

    config = WorkspaceConfig(
        workspace="t",
        repos={"shared": RepoConfig(path=Path("./shared"), type="library")},
    )
    pm = PhaseManager(
        mgr, MagicMock(spec=LogManager),
        config=config, workspace_root=tmp_path,
    )
    # Task has no WorkItem; bypass the (unrelated) WorkItem gate to isolate
    # the blessed-spec-path warning behavior under test.
    result = pm.transition("add-labels", "dev", bypass_spec_gate=True)
    assert not any("spec" in w.lower() for w in result.warnings), result.warnings


def test_spec_new_id_override(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--title", "Something", "--id", "my-id"])
    assert result.exit_code == 0, result.output
    assert _store(configured_app_with_task).find_by_id("my-id") is not None


def test_spec_new_title_overrides_task_description(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--task", "add-labels", "--title", "Override"])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("add-labels")
    assert spec.title == "Override"          # explicit title wins over task.description
    assert spec.task_slug == "add-labels"    # still bound + prefilled


def test_spec_new_empty_title_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "new", "--title", ""])
    assert result.exit_code != 0


def test_spec_new_json_output_non_tty(configured_app_with_task: Path, monkeypatch):
    import json
    from mship.cli.output import Output
    monkeypatch.setattr(Output, "is_tty", property(lambda self: False))
    result = runner.invoke(app, ["spec", "new", "--title", "Json Spec"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == "json-spec"
    assert payload["status"] == "drafting"
    assert "path" in payload


def test_gate_dev_hint_mentions_spec_new(tmp_path: Path):
    """The empty-workspace warning points at `mship spec new`."""
    from unittest.mock import MagicMock
    from mship.core.config import RepoConfig, WorkspaceConfig
    from mship.core.log import LogManager
    from mship.core.phase import PhaseManager
    from mship.core.state import StateManager, Task, WorkspaceState

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="d",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
        worktrees={"shared": tmp_path / "shared"},
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))
    config = WorkspaceConfig(
        workspace="t",
        repos={"shared": RepoConfig(path=Path("./shared"), type="library")},
    )
    pm = PhaseManager(
        mgr, MagicMock(spec=LogManager),
        config=config, workspace_root=tmp_path,
    )
    # Task has no WorkItem; bypass the (unrelated) WorkItem gate to isolate
    # the missing-spec warning hint under test.
    result = pm.transition("add-labels", "dev", bypass_spec_gate=True)
    spec_warn = next((w for w in result.warnings if "spec" in w.lower()), None)
    assert spec_warn is not None, result.warnings
    assert "mship spec new" in spec_warn


def test_spec_draft_emits_prompt(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    result = runner.invoke(app, ["spec", "draft", "dq", "--from-text", "rambled intent here"])
    assert result.exit_code == 0, result.output
    assert "rambled intent here" in result.output
    assert "mship spec apply dq --from-json" in result.output


def test_spec_draft_bare_emits_generic_prompt_both_sources_error(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    # Bare invocation now emits a generic drafting prompt (MOS-184).
    bare = runner.invoke(app, ["spec", "draft", "dq"])
    assert bare.exit_code == 0
    assert "dq" in bare.output
    # Supplying both sources at once is still rejected.
    both = runner.invoke(app, ["spec", "draft", "dq", "--from-text", "x", "--from-file", "f.md"])
    assert both.exit_code != 0


def test_spec_draft_unknown_id_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["spec", "draft", "nope", "--from-text", "x"])
    assert result.exit_code != 0
    assert "nope" in result.output


# --- spec apply (#146) ---

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
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"; jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])           # -> needs_review
    again = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])    # needs_review->needs_review illegal
    assert again.exit_code != 0
    forced = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf), "--bypass-status-gate"])
    assert forced.exit_code == 0, forced.output


# --- spec validate (#146) ---


def test_spec_validate_passes_on_applied_spec(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"; jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    result = runner.invoke(app, ["spec", "validate", "dq"])
    assert result.exit_code == 0, result.output


def test_spec_validate_flags_missing_section(configured_app_with_task: Path):
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


def test_spec_apply_rejects_malformed_json(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "broken.json"
    jf.write_text("this is not json at all")        # JSONDecodeError branch
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    assert result.exit_code != 0


def test_spec_apply_reads_stdin(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    result = runner.invoke(
        app, ["spec", "apply", "dq", "--from-json", "-"], input=_draft_json()
    )
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.status == "needs_review"


def test_spec_draft_missing_file_errors(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    result = runner.invoke(app, ["spec", "draft", "dq", "--from-file", "/no/such/file.md"])
    assert result.exit_code != 0
    assert "from-file" in result.output or "read" in result.output.lower()


def test_spec_apply_missing_file_errors(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", "/no/such/file.json"])
    assert result.exit_code != 0
    assert "from-json" in result.output or "read" in result.output.lower()


# --- spec review (#147) ---


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


# --- spec verdict (#147) ---


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


# --- spec evidence (AC evidence loop) ---


def test_spec_evidence_infers_kind_and_persists(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)  # seeds ac1
    result = runner.invoke(app, ["spec", "evidence", "dq", "ac1", "test-runs/5"])
    assert result.exit_code == 0, result.output
    ac = _store(configured_app_with_task).find_by_id("dq").acceptance_criteria[0]
    assert [(e.kind, e.ref) for e in ac.evidence] == [("test", "test-runs/5")]


def test_spec_evidence_kind_override_and_note(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    result = runner.invoke(
        app, ["spec", "evidence", "dq", "ac1", "HEAD", "--kind", "commit", "--note", "the fix"],
    )
    assert result.exit_code == 0, result.output
    ac = _store(configured_app_with_task).find_by_id("dq").acceptance_criteria[0]
    assert ac.evidence[0].kind == "commit" and ac.evidence[0].note == "the fix"


def test_spec_evidence_unknown_criterion_errors(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "evidence", "dq", "ac99", "test-runs/1"])
    assert result.exit_code != 0
    assert "ac99" in result.output


def test_spec_review_human_shows_evidence_and_unverified(configured_app_with_task: Path, tmp_path, monkeypatch):
    from mship.cli.output import Output
    monkeypatch.setattr(Output, "is_tty", property(lambda self: True))
    _apply_dq(tmp_path)   # ac1, no evidence yet
    runner.invoke(app, ["spec", "evidence", "dq", "ac1", "test-runs/7"])
    result = runner.invoke(app, ["spec", "review", "dq"])
    assert result.exit_code == 0, result.output
    assert "test-runs/7" in result.output          # the evidence ref is shown
    assert "unverified" in result.output.lower()    # summary carries the count


# --- spec ask / answer / questions (#148) ---


def test_spec_ask_adds_question(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)  # seeds q1 (from _draft_json open_questions)
    result = runner.invoke(app, ["spec", "ask", "dq", "Should we support tablets?"])
    assert result.exit_code == 0, result.output
    qs = _json.loads(runner.invoke(app, ["spec", "questions", "dq"]).output)
    assert [q["id"] for q in qs] == ["q1", "q2"]


def test_spec_answer_sets_and_status_unchanged(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    runner.invoke(app, ["spec", "answer", "dq", "q1", "yes"])
    review = _json.loads(runner.invoke(app, ["spec", "review", "dq"]).output)
    assert review["open_questions"][0]["answer"] == "yes"
    assert review["status"] == "needs_review"  # answering didn't transition status
    qs = _json.loads(runner.invoke(app, ["spec", "questions", "dq"]).output)
    assert qs[0]["answer"] == "yes"


def test_spec_answer_unknown_question_errors(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "answer", "dq", "q99", "x"])
    assert result.exit_code != 0
    assert "q99" in result.output


# --- spec approve / request-changes (A5) ---


def test_spec_approve_refused_while_unreviewed(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)  # ac1 unreviewed, q1 unanswered
    result = runner.invoke(app, ["spec", "approve", "dq"])
    assert result.exit_code != 0
    assert "ac1" in result.output


def test_spec_approve_succeeds_when_clear(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    runner.invoke(app, ["spec", "verdict", "dq", "ac1", "approved"])
    runner.invoke(app, ["spec", "answer", "dq", "q1", "yes"])
    result = runner.invoke(app, ["spec", "approve", "dq"])
    assert result.exit_code == 0, result.output
    assert _store(configured_app_with_task).find_by_id("dq").status == "approved"


def test_spec_approve_bypass_gate(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)  # still blocked
    result = runner.invoke(app, ["spec", "approve", "dq", "--bypass-gate"])
    assert result.exit_code == 0, result.output
    assert _store(configured_app_with_task).find_by_id("dq").status == "approved"


def test_spec_approve_rejected_from_wrong_status(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    runner.invoke(app, ["spec", "verdict", "dq", "ac1", "approved"])
    runner.invoke(app, ["spec", "answer", "dq", "q1", "yes"])
    runner.invoke(app, ["spec", "approve", "dq"])              # -> approved
    again = runner.invoke(app, ["spec", "approve", "dq"])      # approved -> approved illegal
    assert again.exit_code != 0


def test_spec_request_changes(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "request-changes", "dq", "--reason", "tighten scope"])
    assert result.exit_code == 0, result.output
    assert _store(configured_app_with_task).find_by_id("dq").status == "needs_clarification"


def test_spec_request_changes_persists_reason_and_logs(configured_app_with_task: Path, tmp_path):
    """MOS-215: the reason must land on the spec itself (not just the task
    log), so `spec show`/`review` can surface it without digging into logs."""
    from mship.core.log import LogManager

    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "request-changes", "dq", "--reason", "tighten scope"])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.clarification_reason == "tighten scope"

    # The log entry is still written alongside the persisted field.
    log = LogManager(configured_app_with_task / ".mothership" / "logs")
    entries = log.read("dq", last=50)
    assert any("tighten scope" in e.message for e in entries)


def test_spec_apply_revising_clears_clarification_reason(configured_app_with_task: Path, tmp_path):
    """Applying a revised draft moves needs_clarification -> needs_review; the
    stale reason from the earlier request-changes must not linger."""
    _apply_dq(tmp_path)
    runner.invoke(app, ["spec", "request-changes", "dq", "--reason", "tighten scope"])
    assert _store(configured_app_with_task).find_by_id("dq").clarification_reason == "tighten scope"

    jf = tmp_path / "revised.json"
    jf.write_text(_draft_json())
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.status == "needs_review"
    assert spec.clarification_reason is None


def test_spec_approve_clears_clarification_reason(configured_app_with_task: Path, tmp_path):
    """Invariant guard (Greptile): an approved spec carries no stale
    request-changes reason. The normal flow clears it on apply; this also
    clears it on the approve path so a needs_review spec that still carries a
    reason (e.g. seeded/legacy state) doesn't get approved with it lingering."""
    store = _store(configured_app_with_task)
    _apply_dq(tmp_path)  # dq -> needs_review
    spec = store.find_by_id("dq")
    spec.clarification_reason = "tighten scope"  # simulate a lingering reason on needs_review
    store.save(spec)

    result = runner.invoke(app, ["spec", "approve", "dq", "--bypass-gate"])
    assert result.exit_code == 0, result.output
    spec = store.find_by_id("dq")
    assert spec.status == "approved"
    assert spec.clarification_reason is None


def test_spec_show_includes_clarification_reason(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    runner.invoke(app, ["spec", "request-changes", "dq", "--reason", "tighten scope"])
    result = runner.invoke(app, ["spec", "show", "dq"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["clarification_reason"] == "tighten scope"


def test_spec_review_includes_clarification_reason(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    runner.invoke(app, ["spec", "request-changes", "dq", "--reason", "tighten scope"])
    result = runner.invoke(app, ["spec", "review", "dq"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["clarification_reason"] == "tighten scope"


# --- spec dispatch (A6 + B4 auto-spawn) ---
# The spec must be approved; the task binds spec_id; spec transitions to "dispatched".
# When no task exists, dispatch auto-spawns one (worktrees per affected_repos).


def _approve_add_labels(workspace: Path, tmp_path: Path) -> None:
    """Create, apply, and approve a spec with id='add-labels' (matches the seeded task)."""
    runner.invoke(app, ["spec", "new", "--title", "Add labels to tasks", "--id", "add-labels"])
    jf = tmp_path / "al_draft.json"
    jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "add-labels", "--from-json", str(jf)])
    runner.invoke(app, ["spec", "verdict", "add-labels", "ac1", "approved"])
    runner.invoke(app, ["spec", "answer", "add-labels", "q1", "yes"])
    runner.invoke(app, ["spec", "approve", "add-labels"])


def test_spec_dispatch_exits_zero_and_sets_dispatched(configured_app_with_task: Path, tmp_path: Path):
    """Happy path: approved spec + matching task → status=dispatched, task.spec_id set, output has AC text."""
    _approve_add_labels(configured_app_with_task, tmp_path)
    result = runner.invoke(app, ["spec", "dispatch", "add-labels"])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("add-labels")
    assert spec.status == "dispatched"
    assert spec.task_slug == "add-labels"
    # Output should contain the acceptance criterion text from _draft_json
    assert "view questions" in result.output


def test_spec_dispatch_binds_spec_id_on_task(configured_app_with_task: Path, tmp_path: Path):
    """spec dispatch must set task.spec_id = spec.id in workspace state."""
    _approve_add_labels(configured_app_with_task, tmp_path)
    runner.invoke(app, ["spec", "dispatch", "add-labels"])
    state_dir = configured_app_with_task / ".mothership"
    mgr = StateManager(state_dir)
    state = mgr.load()
    assert state.tasks["add-labels"].spec_id == "add-labels"


def test_spec_dispatch_requires_approved_status(configured_app_with_task: Path, tmp_path: Path):
    """Dispatching a spec that is not approved must exit non-zero."""
    # Create spec but only apply (status=needs_review, not approved)
    runner.invoke(app, ["spec", "new", "--title", "Add labels to tasks", "--id", "add-labels"])
    jf = tmp_path / "al_draft.json"
    jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "add-labels", "--from-json", str(jf)])
    result = runner.invoke(app, ["spec", "dispatch", "add-labels"])
    assert result.exit_code != 0
    assert "approve" in result.output.lower()


def test_spec_dispatch_auto_spawns_when_no_task(configured_app_git_no_task: Path, tmp_path: Path):
    """No matching task → dispatch auto-spawns one (real worktrees) and dispatches."""
    runner.invoke(app, ["spec", "new", "--title", "Cap feature", "--id", "capfeat"])
    draft = _json.dumps({
        "problem": "P", "user_story": "U", "approach": "A",
        "acceptance_criteria": ["view"], "open_questions": [], "affected_repos": ["shared"],
    })
    jf = tmp_path / "cap.json"
    jf.write_text(draft)
    runner.invoke(app, ["spec", "apply", "capfeat", "--from-json", str(jf)])
    runner.invoke(app, ["spec", "approve", "capfeat", "--bypass-gate"])

    result = runner.invoke(app, ["spec", "dispatch", "capfeat"])
    assert result.exit_code == 0, result.output

    state = StateManager(configured_app_git_no_task / ".mothership").load()
    assert "capfeat" in state.tasks                       # task auto-created
    assert state.tasks["capfeat"].spec_id == "capfeat"    # bound
    # real worktree materialized
    assert (configured_app_git_no_task / ".worktrees" / "capfeat" / "shared").exists()
    assert _store(configured_app_git_no_task).find_by_id("capfeat").status == "dispatched"


def test_spec_dispatch_auto_spawn_refused_without_affected_repos(configured_app_with_task: Path, tmp_path: Path):
    """No task + spec has no affected_repos → dispatch refuses (can't auto-spawn)."""
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    draft = _json.dumps({
        "problem": "P", "user_story": "U", "approach": "A",
        "acceptance_criteria": ["x"], "open_questions": [], "affected_repos": [],
    })
    jf = tmp_path / "dq.json"
    jf.write_text(draft)
    runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    runner.invoke(app, ["spec", "approve", "dq", "--bypass-gate"])
    result = runner.invoke(app, ["spec", "dispatch", "dq"])
    assert result.exit_code != 0
    assert "affected_repos" in result.output


def test_spec_dispatch_unknown_id_errors(configured_app_with_task: Path):
    """Dispatching a non-existent spec id must exit non-zero."""
    result = runner.invoke(app, ["spec", "dispatch", "no-such-spec"])
    assert result.exit_code != 0
    assert "no-such-spec" in result.output


# --- spec dispatch: WorkItem-join adopt + refuse-to-guess (MOS-228 T4) ---


def test_spec_dispatch_adopts_via_workitem_join(configured_app_with_task: Path, tmp_path: Path):
    """Spec's WorkItem already has exactly one live candidate task -> dispatch
    adopts it instead of auto-spawning a duplicate task/WorkItem."""
    from mship.core.workitem_store import WorkItemStore

    state_dir = configured_app_with_task / ".mothership"
    mgr = StateManager(state_dir)
    now = datetime(2026, 4, 10, tzinfo=timezone.utc)
    state = mgr.load()
    state.tasks["other-task"] = Task(
        slug="other-task", description="d", phase="plan", created_at=now,
        affected_repos=["shared"], branch="feat/other-task",
    )
    mgr.save(state)

    items = WorkItemStore(state_dir / "workitems")
    wi = items.create(title="Decision queue", kind="feature", workspace="testws", now=now)
    items.add_task(wi.id, "other-task", now=now, state=mgr)

    _apply_dq(tmp_path)
    runner.invoke(app, ["spec", "verdict", "dq", "ac1", "approved"])
    runner.invoke(app, ["spec", "answer", "dq", "q1", "yes"])
    runner.invoke(app, ["spec", "approve", "dq"])

    store = _store(configured_app_with_task)
    spec = store.find_by_id("dq")
    spec.work_item_id = wi.id
    store.save(spec)

    result = runner.invoke(app, ["spec", "dispatch", "dq"])
    assert result.exit_code == 0, result.output

    assert store.find_by_id("dq").task_slug == "other-task"
    state_after = StateManager(state_dir).load()
    assert state_after.tasks["other-task"].spec_id == "dq"
    assert "dq" not in state_after.tasks                  # no auto-spawned duplicate task
    assert [i.id for i in items.list()] == [wi.id]        # no new WorkItem minted


def test_spec_dispatch_refuses_to_guess_via_workitem_join(configured_app_with_task: Path, tmp_path: Path):
    """Spec's WorkItem has >=2 live candidate tasks -> dispatch refuses with a
    clean non-zero exit (not a bare traceback) naming --task, mutating nothing."""
    from mship.core.workitem_store import WorkItemStore

    state_dir = configured_app_with_task / ".mothership"
    mgr = StateManager(state_dir)
    now = datetime(2026, 4, 10, tzinfo=timezone.utc)
    state = mgr.load()
    state.tasks["t1"] = Task(slug="t1", description="d", phase="plan", created_at=now,
                              affected_repos=["shared"], branch="feat/t1")
    state.tasks["t2"] = Task(slug="t2", description="d", phase="plan", created_at=now,
                              affected_repos=["shared"], branch="feat/t2")
    mgr.save(state)

    items = WorkItemStore(state_dir / "workitems")
    wi = items.create(title="Decision queue", kind="feature", workspace="testws", now=now)
    items.add_task(wi.id, "t1", now=now, state=mgr)
    items.add_task(wi.id, "t2", now=now, state=mgr)

    _apply_dq(tmp_path)
    runner.invoke(app, ["spec", "verdict", "dq", "ac1", "approved"])
    runner.invoke(app, ["spec", "answer", "dq", "q1", "yes"])
    runner.invoke(app, ["spec", "approve", "dq"])

    store = _store(configured_app_with_task)
    spec = store.find_by_id("dq")
    spec.work_item_id = wi.id
    store.save(spec)

    result = runner.invoke(app, ["spec", "dispatch", "dq"])

    assert result.exit_code != 0
    assert "--task" in result.output
    assert isinstance(result.exception, SystemExit)  # clean typer.Exit, no bare traceback

    assert store.find_by_id("dq").status == "approved"                    # unchanged
    assert sorted(StateManager(state_dir).load().tasks) == ["add-labels", "t1", "t2"]
    assert [i.id for i in items.list()] == [wi.id]                        # no new WorkItem
    assert sorted(items.get(wi.id).task_slugs) == ["t1", "t2"]            # untouched


# --- spec from-thread (#capture-as-conversation) ---


@pytest.fixture
def _configured(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    yield workspace

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_spec_from_thread_creates_links_and_prompts(_configured):
    from datetime import datetime, timezone
    from mship.core.message_store import MessageStore
    from mship.core.spec_store import SpecStore, SPECS_DIRNAME
    ws = _configured
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    mstore = MessageStore(ws / ".mothership" / "messages")
    t = mstore.create_thread(subject="Add dark mode", text="we should add dark mode", now=now)
    mstore.append(t.id, "agent", "which screens?", now)

    result = runner.invoke(app, ["spec", "from-thread", t.id])
    assert result.exit_code == 0, result.output
    # a spec was created, titled from the subject, and linked to the thread
    spec = SpecStore(ws / SPECS_DIRNAME).find_by_id(mstore.get(t.id).spec_id)
    assert spec is not None and spec.title == "Add dark mode"
    # the printed drafting prompt embeds the transcript
    assert "we should add dark mode" in result.output
    assert "which screens?" in result.output


def test_spec_from_thread_unknown_thread_errors(_configured):
    assert runner.invoke(app, ["spec", "from-thread", "nope"]).exit_code != 0


def test_spec_from_thread_is_idempotent_and_does_not_orphan(_configured):
    from datetime import datetime, timezone
    from mship.core.message_store import MessageStore
    from mship.core.spec_store import SpecStore, SPECS_DIRNAME
    ws = _configured
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    mstore = MessageStore(ws / ".mothership" / "messages")
    t = mstore.create_thread(subject="Add dark mode", text="we should add dark mode", now=now)

    first = runner.invoke(app, ["spec", "from-thread", t.id])
    assert first.exit_code == 0, first.output
    linked = mstore.get(t.id).spec_id

    # A second invocation must reuse the linked spec, not create a new one.
    second = runner.invoke(app, ["spec", "from-thread", t.id])
    assert second.exit_code == 0, second.output
    assert mstore.get(t.id).spec_id == linked  # link unchanged
    store = SpecStore(ws / SPECS_DIRNAME)
    assert len(store.list()) == 1  # no orphaned spec
    assert "reusing spec" in second.output
