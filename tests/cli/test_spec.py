"""Tests for `mship spec new` (#126, #145)."""
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core import spec_key
from mship.core.spec import Spec
from mship.core.spec_storage import SpecStorage
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
    assert spec.status == "draft"
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



def test_spec_new_refuses_duplicate_locked_spec(configured_app_with_task: Path):
    workspace = configured_app_with_task
    (workspace / "mothership.yaml").write_text(
        "workspace: test\nrepos: {}\nspec_storage: encrypted\n",
    )
    now = datetime.now(timezone.utc)
    encrypted = SpecStorage(workspace / "specs", mode="encrypted", workspace_root=workspace)
    SpecStore(workspace / "specs", storage=encrypted).save(Spec(
        id="locked-spec", title="Locked", status="draft", created_at=now, updated_at=now,
    ))
    spec_key.keyfile_path(workspace).unlink()
    container.config.reset()

    result = runner.invoke(app, ["spec", "new", "--id", "locked-spec", "--title", "Replacement"])

    assert result.exit_code != 0
    assert "locked" in result.output.lower()


def test_spec_new_duplicate_reports_the_existing_encrypted_artifact(
    configured_app_with_task: Path,
):
    workspace = configured_app_with_task
    (workspace / "mothership.yaml").write_text(
        "workspace: test\nrepos: {}\nspec_storage: encrypted\n",
    )
    created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    encrypted = SpecStorage(workspace / "specs", mode="encrypted", workspace_root=workspace)
    existing = SpecStore(workspace / "specs", storage=encrypted).save(Spec(
        id="existing-spec",
        title="Existing",
        status="draft",
        created_at=created,
        updated_at=created,
    ))
    container.config.reset()

    result = runner.invoke(
        app,
        ["spec", "new", "--id", "existing-spec", "--title", "Replacement"],
    )

    assert result.exit_code != 0
    assert str(existing) in result.output
    assert existing.name.endswith(".md.enc")

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
    assert payload["status"] == "draft"
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


def _draft_json(acceptance_criteria: list[str] | None = None) -> str:
    return _json.dumps({
        "problem": "P", "user_story": "U", "approach": "A",
        "acceptance_criteria": acceptance_criteria or ["view questions"],
        "open_questions": ["Android?"], "non_goals": ["chat"], "risks": [],
        "affected_repos": ["mothership"],
    })


def _apply_reviewed_spec(workspace: Path, tmp_path: Path) -> Path:
    runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    draft = tmp_path / "draft.json"
    draft.write_text(_draft_json(["view questions", "sync status"]))
    assert runner.invoke(
        app, ["spec", "apply", "add-labels", "--from-json", str(draft)],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "verdict", "add-labels", "ac1", "approved"],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "verdict", "add-labels", "ac2", "flagged"],
    ).exit_code == 0
    store = _store(workspace)
    spec = store.find_by_id("add-labels")
    assert spec is not None
    return store.path_for(spec)

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


@pytest.mark.parametrize("spec_id", ["../unsafe", "nul\x00unsafe"])
def test_spec_apply_rejects_unsafe_spec_id_without_traceback(
    configured_app_with_task: Path, tmp_path: Path, spec_id: str,
):
    draft = tmp_path / "draft.json"
    draft.write_text(_draft_json())

    result = runner.invoke(
        app, ["spec", "apply", spec_id, "--from-json", str(draft)],
    )

    assert result.exit_code != 0
    assert "unsafe spec id" in result.output
    assert "Traceback" not in result.output

def test_spec_apply_refuses_wrong_status(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"; jf.write_text(_draft_json())
    runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])           # -> needs_review
    again = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])    # needs_review->needs_review illegal
    assert again.exit_code != 0
    forced = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf), "--bypass-status-gate"])
    assert forced.exit_code == 0, forced.output


def test_spec_apply_refuses_reviewed_spec_without_discarding_review(
    configured_app_with_task: Path, tmp_path: Path,
):
    artifact = _apply_reviewed_spec(configured_app_with_task, tmp_path)
    before = artifact.read_bytes()
    activity_before = StateManager(
        configured_app_with_task / ".mothership",
    ).load().tasks["add-labels"].last_activity_at
    replacement = tmp_path / "replacement.json"
    replacement.write_text(_draft_json(["new criterion"]))

    ordinary = runner.invoke(
        app, ["spec", "apply", "add-labels", "--from-json", str(replacement)],
    )
    assert ordinary.exit_code != 0
    assert "2 review units" in ordinary.output
    assert "discard-review" in ordinary.output
    assert artifact.read_bytes() == before
    assert StateManager(
        configured_app_with_task / ".mothership",
    ).load().tasks["add-labels"].last_activity_at == activity_before

    bypassed = runner.invoke(
        app,
        [
            "spec", "apply", "add-labels", "--from-json", str(replacement),
            "--bypass-status-gate",
        ],
    )
    assert bypassed.exit_code != 0
    assert "2 review units" in bypassed.output
    assert "discard-review" in bypassed.output
    assert artifact.read_bytes() == before
    assert StateManager(
        configured_app_with_task / ".mothership",
    ).load().tasks["add-labels"].last_activity_at == activity_before


def test_spec_apply_refuses_reviewed_prose_without_discarding_review(
    configured_app_with_task: Path, tmp_path: Path,
):
    runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    initial = tmp_path / "initial.json"
    initial.write_text(_draft_json())
    assert runner.invoke(
        app, ["spec", "apply", "add-labels", "--from-json", str(initial)],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "verdict", "add-labels", "problem", "approved"],
    ).exit_code == 0
    store = _store(configured_app_with_task)
    spec = store.find_by_id("add-labels")
    assert spec is not None
    assert [criterion.verdict for criterion in spec.acceptance_criteria] == ["unreviewed"]
    artifact = store.path_for(spec)
    before = artifact.read_bytes()
    activity_before = StateManager(
        configured_app_with_task / ".mothership",
    ).load().tasks["add-labels"].last_activity_at
    replacement = tmp_path / "replacement.json"
    replacement.write_text(_json.dumps({
        **_json.loads(_draft_json()),
        "problem": "Replacement problem",
    }))

    ordinary = runner.invoke(
        app, ["spec", "apply", "add-labels", "--from-json", str(replacement)],
    )
    assert ordinary.exit_code != 0
    assert "1 review unit" in ordinary.output
    assert "discard-review" in ordinary.output
    assert artifact.read_bytes() == before
    assert StateManager(
        configured_app_with_task / ".mothership",
    ).load().tasks["add-labels"].last_activity_at == activity_before

    bypassed = runner.invoke(
        app,
        [
            "spec", "apply", "add-labels", "--from-json", str(replacement),
            "--bypass-status-gate",
        ],
    )
    assert bypassed.exit_code != 0
    assert "1 review unit" in bypassed.output
    assert "discard-review" in bypassed.output
    assert artifact.read_bytes() == before
    assert StateManager(
        configured_app_with_task / ".mothership",
    ).load().tasks["add-labels"].last_activity_at == activity_before

    discarded = runner.invoke(
        app,
        [
            "spec", "apply", "add-labels", "--from-json", str(replacement),
            "--bypass-status-gate", "--discard-review",
        ],
    )
    assert discarded.exit_code == 0, discarded.output
    payload = _json.loads(discarded.output)
    assert payload["discarded_review_count"] == 1
    assert "Replacement problem" not in discarded.output
    spec = store.find_by_id("add-labels")
    assert spec is not None
    assert spec.prose_verdicts == {}


def test_spec_apply_discard_review_resets_replaced_json_criteria(
    configured_app_with_task: Path, tmp_path: Path,
):
    _apply_reviewed_spec(configured_app_with_task, tmp_path)
    replacement = tmp_path / "replacement.json"
    replacement.write_text(_draft_json(["new criterion"]))

    result = runner.invoke(
        app,
        [
            "spec", "apply", "add-labels", "--from-json", str(replacement),
            "--bypass-status-gate", "--discard-review",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["review_state_discarded"] is True
    assert payload["discarded_review_count"] == 2
    assert "new criterion" not in result.output
    spec = _store(configured_app_with_task).find_by_id("add-labels")
    assert [c.verdict for c in spec.acceptance_criteria] == ["unreviewed"]


def test_spec_apply_discard_review_preserves_unreviewed_criterion_evidence(
    configured_app_with_task: Path, tmp_path: Path,
):
    runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    initial = tmp_path / "initial.json"
    initial.write_text(_draft_json(["replace criterion", "keep criterion"]))
    assert runner.invoke(
        app, ["spec", "apply", "add-labels", "--from-json", str(initial)],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "verdict", "add-labels", "ac1", "approved"],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "evidence", "add-labels", "ac2", "test-runs/1"],
    ).exit_code == 0
    replacement = tmp_path / "replacement.json"
    replacement.write_text(_draft_json(["replacement criterion", "keep criterion"]))

    result = runner.invoke(
        app,
        [
            "spec", "apply", "add-labels", "--from-json", str(replacement),
            "--bypass-status-gate", "--discard-review",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["discarded_review_count"] == 1
    spec = _store(configured_app_with_task).find_by_id("add-labels")
    assert [criterion.verdict for criterion in spec.acceptance_criteria] == [
        "unreviewed", "unreviewed",
    ]
    assert [evidence.ref for evidence in spec.acceptance_criteria[1].evidence] == [
        "test-runs/1",
    ]


def test_spec_apply_preserves_unchanged_review_state_and_answers(
    configured_app_with_task: Path, tmp_path: Path,
):
    runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    draft = tmp_path / "draft.json"
    draft.write_text(_draft_json())
    assert runner.invoke(
        app, ["spec", "apply", "add-labels", "--from-json", str(draft)],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "verdict", "add-labels", "ac1", "approved"],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "evidence", "add-labels", "ac1", "test-runs/1"],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "verdict", "add-labels", "problem", "flagged"],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "answer", "add-labels", "q1", "Keep Android support"],
    ).exit_code == 0
    store = _store(configured_app_with_task)
    spec = store.find_by_id("add-labels")
    assert spec is not None
    spec.acceptance_criteria[0].comment = "criterion review"
    spec.prose_verdicts["problem"].comment = "prose review"
    store.save(spec)

    result = runner.invoke(
        app,
        [
            "spec", "apply", "add-labels", "--from-json", str(draft),
            "--bypass-status-gate",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "review_state_discarded" not in _json.loads(result.output)
    persisted = store.find_by_id("add-labels")
    assert persisted is not None
    assert persisted.acceptance_criteria[0].verdict == "approved"
    assert persisted.acceptance_criteria[0].comment == "criterion review"
    assert [evidence.ref for evidence in persisted.acceptance_criteria[0].evidence] == [
        "test-runs/1",
    ]
    assert persisted.prose_verdicts["problem"].verdict == "flagged"
    assert persisted.prose_verdicts["problem"].comment == "prose review"
    assert persisted.open_questions[0].answer == "Keep Android support"


def test_spec_apply_refuses_to_discard_an_answered_question(
    configured_app_with_task: Path, tmp_path: Path,
):
    runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    initial = tmp_path / "initial.json"
    initial.write_text(_draft_json())
    assert runner.invoke(
        app, ["spec", "apply", "add-labels", "--from-json", str(initial)],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "answer", "add-labels", "q1", "Keep Android support"],
    ).exit_code == 0
    replacement = tmp_path / "replacement.json"
    replacement.write_text(_json.dumps({
        **_json.loads(_draft_json()),
        "open_questions": ["Should we support iOS?"],
    }))

    refused = runner.invoke(
        app,
        [
            "spec", "apply", "add-labels", "--from-json", str(replacement),
            "--bypass-status-gate",
        ],
    )

    assert refused.exit_code != 0
    assert "1 review unit" in refused.output
    assert "discard-review" in refused.output
    preserved = _store(configured_app_with_task).find_by_id("add-labels")
    assert preserved is not None
    assert preserved.open_questions[0].answer == "Keep Android support"

    discarded = runner.invoke(
        app,
        [
            "spec", "apply", "add-labels", "--from-json", str(replacement),
            "--bypass-status-gate", "--discard-review",
        ],
    )

    assert discarded.exit_code == 0, discarded.output
    payload = _json.loads(discarded.output)
    assert payload["discarded_review_count"] == 1
    assert "Should we support iOS?" not in discarded.output
    replaced = _store(configured_app_with_task).find_by_id("add-labels")
    assert replaced is not None
    assert replaced.open_questions[0].answer is None


def test_spec_verdict_does_not_overwrite_an_apply_from_a_stale_snapshot(
    configured_app_with_task: Path, tmp_path: Path, monkeypatch,
):
    runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    initial = tmp_path / "initial.json"
    initial.write_text(_draft_json(["old criterion"]))
    assert runner.invoke(
        app, ["spec", "apply", "add-labels", "--from-json", str(initial)],
    ).exit_code == 0
    assert runner.invoke(
        app, ["spec", "verdict", "add-labels", "ac1", "approved"],
    ).exit_code == 0
    store = _store(configured_app_with_task)
    stale_spec = store.find_by_id("add-labels")
    assert stale_spec is not None
    replacement = tmp_path / "replacement.json"
    replacement.write_text(_draft_json(["new criterion"]))

    original_save_while_locked = SpecStore.save_while_locked
    apply_ready_to_persist = Event()
    allow_apply_to_persist = Event()

    def pause_apply_persistence(self, spec, artifact):
        if (
            spec.id == "add-labels"
            and spec.acceptance_criteria[0].text == "new criterion"
        ):
            apply_ready_to_persist.set()
            assert allow_apply_to_persist.wait(timeout=5)
        return original_save_while_locked(self, spec, artifact)

    monkeypatch.setattr(SpecStore, "save_while_locked", pause_apply_persistence)

    from typer.main import get_command

    callbacks = get_command(app).commands["spec"].commands
    apply_callback = callbacks["apply"].callback
    verdict_callback = callbacks["verdict"].callback
    assert apply_callback is not None
    assert verdict_callback is not None

    apply_result = {}

    def apply_draft():
        try:
            apply_callback(
                spec_id="add-labels",
                from_json=str(replacement),
                from_file=None,
                bypass_status_gate=True,
                discard_review=True,
            )
        except BaseException as exc:
            apply_result["error"] = exc

    apply_thread = Thread(target=apply_draft)
    apply_thread.start()
    assert apply_ready_to_persist.wait(timeout=5)

    original_find_by_id = SpecStore.find_by_id

    def stale_find_by_id(self, spec_id):
        if spec_id == "add-labels":
            return stale_spec.model_copy(deep=True)
        return original_find_by_id(self, spec_id)

    monkeypatch.setattr(SpecStore, "find_by_id", stale_find_by_id)
    verdict_result = {}

    def record_verdict():
        try:
            verdict_callback(
                spec_id="add-labels",
                criterion_id="ac1",
                verdict_value="approved",
            )
        except BaseException as exc:
            verdict_result["error"] = exc

    verdict_thread = Thread(target=record_verdict)
    verdict_thread.start()
    allow_apply_to_persist.set()
    apply_thread.join(timeout=5)
    verdict_thread.join(timeout=5)

    assert not apply_thread.is_alive()
    assert not verdict_thread.is_alive()
    assert "error" not in apply_result
    assert "error" not in verdict_result
    persisted = original_find_by_id(store, "add-labels")
    assert persisted is not None
    assert persisted.acceptance_criteria[0].text == "new criterion"
    assert persisted.acceptance_criteria[0].verdict == "approved"


def test_spec_ask_does_not_overwrite_an_apply_from_a_stale_snapshot(
    configured_app_with_task: Path, tmp_path: Path, monkeypatch,
):
    from threading import current_thread
    from typer.main import get_command

    runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    initial = tmp_path / "initial.json"
    initial.write_text(_draft_json(["old criterion"]))
    assert runner.invoke(
        app, ["spec", "apply", "add-labels", "--from-json", str(initial)],
    ).exit_code == 0
    replacement = tmp_path / "replacement.json"
    replacement.write_text(_draft_json(["new criterion"]))

    original_save_while_locked = SpecStore.save_while_locked
    apply_ready_to_persist = Event()
    allow_apply_to_persist = Event()

    def pause_apply_persistence(self, spec, artifact):
        if (
            spec.id == "add-labels"
            and spec.acceptance_criteria[0].text == "new criterion"
        ):
            apply_ready_to_persist.set()
            assert allow_apply_to_persist.wait(timeout=5)
        return original_save_while_locked(self, spec, artifact)

    monkeypatch.setattr(SpecStore, "save_while_locked", pause_apply_persistence)

    callbacks = get_command(app).commands["spec"].commands
    apply_callback = callbacks["apply"].callback
    ask_callback = callbacks["ask"].callback
    assert apply_callback is not None
    assert ask_callback is not None

    apply_result = {}

    def apply_draft():
        try:
            apply_callback(
                spec_id="add-labels",
                from_json=str(replacement),
                from_file=None,
                bypass_status_gate=True,
                discard_review=True,
            )
        except BaseException as exc:
            apply_result["error"] = exc

    apply_thread = Thread(target=apply_draft)
    apply_thread.start()
    assert apply_ready_to_persist.wait(timeout=5)

    original_locked = SpecStore.locked
    ask_waiting_for_lock = Event()

    def detect_ask_lock(self, spec_id):
        if current_thread().name == "ask-writer":
            ask_waiting_for_lock.set()
        return original_locked(self, spec_id)

    monkeypatch.setattr(SpecStore, "locked", detect_ask_lock)
    ask_result = {}

    def ask_question():
        try:
            ask_callback(
                spec_id="add-labels",
                text="Should we support tablets?",
            )
        except BaseException as exc:
            ask_result["error"] = exc

    ask_thread = Thread(target=ask_question, name="ask-writer")
    ask_thread.start()
    assert ask_waiting_for_lock.wait(timeout=5)
    allow_apply_to_persist.set()
    apply_thread.join(timeout=5)
    ask_thread.join(timeout=5)

    assert not apply_thread.is_alive()
    assert not ask_thread.is_alive()
    assert "error" not in apply_result
    assert "error" not in ask_result
    persisted = _store(configured_app_with_task).find_by_id("add-labels")
    assert persisted is not None
    assert persisted.acceptance_criteria[0].text == "new criterion"
    assert [question.text for question in persisted.open_questions] == [
        "Android?", "Should we support tablets?",
    ]


def test_spec_answer_does_not_overwrite_an_apply_from_a_stale_snapshot(
    configured_app_with_task: Path, tmp_path: Path, monkeypatch,
):
    from threading import current_thread
    from typer.main import get_command

    runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    initial = tmp_path / "initial.json"
    initial.write_text(_draft_json(["old criterion"]))
    assert runner.invoke(
        app, ["spec", "apply", "add-labels", "--from-json", str(initial)],
    ).exit_code == 0
    replacement = tmp_path / "replacement.json"
    replacement.write_text(_draft_json(["new criterion"]))

    original_save_while_locked = SpecStore.save_while_locked
    apply_ready_to_persist = Event()
    allow_apply_to_persist = Event()

    def pause_apply_persistence(self, spec, artifact):
        if (
            spec.id == "add-labels"
            and spec.acceptance_criteria[0].text == "new criterion"
        ):
            apply_ready_to_persist.set()
            assert allow_apply_to_persist.wait(timeout=5)
        return original_save_while_locked(self, spec, artifact)

    monkeypatch.setattr(SpecStore, "save_while_locked", pause_apply_persistence)

    callbacks = get_command(app).commands["spec"].commands
    apply_callback = callbacks["apply"].callback
    answer_callback = callbacks["answer"].callback
    assert apply_callback is not None
    assert answer_callback is not None

    apply_result = {}

    def apply_draft():
        try:
            apply_callback(
                spec_id="add-labels",
                from_json=str(replacement),
                from_file=None,
                bypass_status_gate=True,
                discard_review=True,
            )
        except BaseException as exc:
            apply_result["error"] = exc

    apply_thread = Thread(target=apply_draft)
    apply_thread.start()
    assert apply_ready_to_persist.wait(timeout=5)

    original_locked = SpecStore.locked
    answer_waiting_for_lock = Event()

    def detect_answer_lock(self, spec_id):
        if current_thread().name == "answer-writer":
            answer_waiting_for_lock.set()
        return original_locked(self, spec_id)

    monkeypatch.setattr(SpecStore, "locked", detect_answer_lock)
    answer_result = {}

    def answer_question():
        try:
            answer_callback(
                spec_id="add-labels",
                q_id="q1",
                answer_text="yes",
            )
        except BaseException as exc:
            answer_result["error"] = exc

    answer_thread = Thread(target=answer_question, name="answer-writer")
    answer_thread.start()
    assert answer_waiting_for_lock.wait(timeout=5)
    allow_apply_to_persist.set()
    apply_thread.join(timeout=5)
    answer_thread.join(timeout=5)

    assert not apply_thread.is_alive()
    assert not answer_thread.is_alive()
    assert "error" not in apply_result
    assert "error" not in answer_result
    persisted = _store(configured_app_with_task).find_by_id("add-labels")
    assert persisted is not None
    assert persisted.acceptance_criteria[0].text == "new criterion"
    assert persisted.open_questions[0].answer == "yes"
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

def test_spec_validate_unsafe_id_is_a_controlled_cli_error(
    configured_app_with_task: Path,
):
    result = runner.invoke(app, ["spec", "validate", ".hidden"])

    assert result.exit_code != 0
    assert "unsafe spec id" in result.output
    assert result.exception is not None
    assert result.exception.__class__.__name__ == "SystemExit"


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


def test_spec_verdict_prose_section_records_and_persists(configured_app_with_task: Path, tmp_path):
    """A prose-section id routes to set_prose_verdict instead of erroring."""
    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "verdict", "dq", "approach", "approved"])
    assert result.exit_code == 0, result.output
    review = _json.loads(runner.invoke(app, ["spec", "review", "dq"]).output)
    assert review["prose_verdicts"]["approach"]["verdict"] == "approved"


def test_spec_verdict_prose_clears_approve_gate(configured_app_with_task: Path, tmp_path):
    """A previously-flagged prose section blocks approve; re-verdicting it via the
    same `spec verdict` command clears the gate."""
    from mship.core.spec_review import set_prose_verdict
    _apply_dq(tmp_path)
    store = _store(configured_app_with_task)
    spec = store.find_by_id("dq")
    set_prose_verdict(spec, "approach", "flagged")
    store.save(spec)
    # Satisfy the other gate legs: AC approved + open question answered.
    assert runner.invoke(app, ["spec", "verdict", "dq", "ac1", "approved"]).exit_code == 0
    assert runner.invoke(app, ["spec", "answer", "dq", "q1", "yes"]).exit_code == 0
    blocked = runner.invoke(app, ["spec", "approve", "dq"])
    assert blocked.exit_code != 0
    assert "approach" in blocked.output
    assert runner.invoke(app, ["spec", "verdict", "dq", "approach", "approved"]).exit_code == 0
    approved = runner.invoke(app, ["spec", "approve", "dq"])
    assert approved.exit_code == 0, approved.output
    assert store.find_by_id("dq").status == "approved"


def test_spec_verdict_prose_rejects_bad_verdict(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "verdict", "dq", "approach", "bogus"])
    assert result.exit_code != 0
    assert "bogus" in result.output


def test_spec_verdict_unknown_id_lists_both_vocabularies(configured_app_with_task: Path, tmp_path):
    """An id that is neither an AC nor a prose section errors listing both."""
    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "verdict", "dq", "nope", "approved"])
    assert result.exit_code != 0
    assert "ac1" in result.output          # acceptance-criterion vocabulary
    assert "approach" in result.output     # prose-section vocabulary


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
    # MOS-240: request-changes sends the spec back to the editable `draft` status
    # (needs_clarification is gone); the ask lives in clarification_reason.
    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.status == "draft"
    assert spec.clarification_reason == "tighten scope"


def test_cli_approve_uses_shared_transition(configured_app_with_task: Path, tmp_path, monkeypatch):
    called = {}
    import mship.core.spec_transition as st
    orig = st.approve_spec

    def spy(spec, store, *, bypass_gate=False):
        called["hit"] = spec.id
        return orig(spec, store, bypass_gate=bypass_gate)
    monkeypatch.setattr(st, "approve_spec", spy)

    _apply_dq(tmp_path)                                    # needs_review spec "dq"
    runner.invoke(app, ["spec", "verdict", "dq", "ac1", "approved"])
    runner.invoke(app, ["spec", "answer", "dq", "q1", "yes"])   # clears the gate
    result = runner.invoke(app, ["spec", "approve", "dq"])
    assert result.exit_code == 0, result.output
    assert called["hit"] == "dq"
    assert _store(configured_app_with_task).find_by_id("dq").status == "approved"


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


def test_spec_request_changes_records_durable_rejection(configured_app_with_task: Path, tmp_path):
    """#447: request-changes must also append a durable, append-only
    `rejected` journal event carrying {actor, reason} as JSON — distinct
    from `clarification_reason`, which a later approve_spec() nulls."""
    import json

    from mship.core.log import LogManager

    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "request-changes", "dq", "--reason", "tighten scope"])
    assert result.exit_code == 0, result.output

    log = LogManager(configured_app_with_task / ".mothership" / "logs")
    rejected = [e for e in log.read("dq") if e.action == "rejected"]
    assert len(rejected) == 1
    payload = json.loads(rejected[0].message)
    assert payload["reason"] == "tighten scope"
    assert payload["actor"]


def test_spec_request_changes_fails_loud_when_journal_write_fails(
    configured_app_with_task: Path, tmp_path, monkeypatch,
):
    """#447 review: a durable-write failure must FAIL LOUD (non-zero exit,
    no success reported) and must NOT leave the spec flipped to draft —
    the record-then-transition ordering means a failed append leaves the
    spec's status untouched, so the operator can retry cleanly."""
    import mship.core.spec_transition as st

    _apply_dq(tmp_path)

    def _boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(st, "record_rejection", _boom)

    result = runner.invoke(app, ["spec", "request-changes", "dq", "--reason", "tighten scope"])
    assert result.exit_code != 0
    assert "success" not in result.output.lower()

    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.status == "needs_review"
    assert spec.clarification_reason is None


def test_spec_request_changes_rejects_empty_reason(configured_app_with_task: Path, tmp_path):
    _apply_dq(tmp_path)
    result = runner.invoke(app, ["spec", "request-changes", "dq", "--reason", "   "])
    assert result.exit_code != 0

    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.status == "needs_review"


def test_spec_apply_revising_clears_clarification_reason(configured_app_with_task: Path, tmp_path):
    """Applying a revised draft moves draft -> needs_review (MOS-240); the
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


def test_spec_apply_stamps_bound_task_activity(configured_app_with_task: Path, tmp_path):
    # `spec new --task add-labels` binds spec.task_slug = "add-labels" (id "add-labels").
    runner.invoke(app, ["spec", "new", "--task", "add-labels"])
    jf = tmp_path / "draft.json"
    jf.write_text(_draft_json())
    result = runner.invoke(app, ["spec", "apply", "add-labels", "--from-json", str(jf)])
    assert result.exit_code == 0, result.output
    state = StateManager(configured_app_with_task / ".mothership").load()
    assert state.tasks["add-labels"].last_activity_at is not None


# --- spec apply --from-file (#298 item 1) ---


def test_spec_apply_discard_review_reports_human_markdown_result(
    configured_app_with_task: Path, tmp_path: Path, monkeypatch,
):
    from mship.cli.output import Output

    monkeypatch.setattr(Output, "is_tty", property(lambda self: True))
    _apply_reviewed_spec(configured_app_with_task, tmp_path)
    replacement = tmp_path / "replacement.md"
    replacement.write_text(_draft_md())

    result = runner.invoke(
        app,
        [
            "spec", "apply", "add-labels", "--from-file", str(replacement),
            "--bypass-status-gate", "--discard-review",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "review state was discarded for 1 review unit" in result.output.lower()
    assert "view questions" not in result.output
    assert "sync status" not in result.output
    spec = _store(configured_app_with_task).find_by_id("add-labels")
    assert [c.verdict for c in spec.acceptance_criteria] == ["approved"]


def _draft_md() -> str:
    return (
        "## Problem\n\nP\n\n"
        "## User story\n\nU\n\n"
        "## Approach\n\nA\n\n"
        "## Acceptance criteria\n\n"
        "- [ ] `ac1` view questions\n\n"
        "## Open questions\n\n"
        "- Android?\n\n"
        "## Non-goals\n\n"
        "- chat\n\n"
        "## Affected repos\n\n"
        "- mothership\n"
    )


def test_spec_apply_from_file_merges_and_advances_status(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    mf = tmp_path / "draft.md"
    mf.write_text(_draft_md())
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-file", str(mf)])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.status == "needs_review"
    assert [c.id for c in spec.acceptance_criteria] == ["ac1"]
    assert spec.acceptance_criteria[0].text == "view questions"
    assert spec.non_goals == ["chat"]
    assert spec.affected_repos == ["mothership"]
    assert "## Problem" in spec.body


def test_spec_apply_requires_exactly_one_source(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    # Neither source.
    neither = runner.invoke(app, ["spec", "apply", "dq"])
    assert neither.exit_code != 0
    assert "exactly one" in neither.output.lower()
    # Both sources.
    jf = tmp_path / "d.json"; jf.write_text(_draft_json())
    mf = tmp_path / "d.md"; mf.write_text(_draft_md())
    both = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf), "--from-file", str(mf)])
    assert both.exit_code != 0
    assert "exactly one" in both.output.lower()


def test_spec_apply_from_file_missing_file_errors(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-file", "/no/such/file.md"])
    assert result.exit_code != 0
    assert "from-file" in result.output or "read" in result.output.lower()


def test_spec_apply_from_file_malformed_markdown_errors(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    mf = tmp_path / "bad.md"
    mf.write_text("just some notes, no headings")  # missing required sections
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-file", str(mf)])
    assert result.exit_code != 0


# --- spec apply --from-json regression guard (#298 item 1) ---


def test_regression_from_json_file_still_applies(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "draft.json"; jf.write_text(_draft_json())
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    assert result.exit_code == 0, result.output
    spec = _store(configured_app_with_task).find_by_id("dq")
    assert spec.status == "needs_review"
    assert [c.id for c in spec.acceptance_criteria] == ["ac1"]
    assert "## Problem" in spec.body


def test_regression_from_json_stdin_still_applies(configured_app_with_task: Path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", "-"], input=_draft_json())
    assert result.exit_code == 0, result.output
    assert _store(configured_app_with_task).find_by_id("dq").status == "needs_review"


def test_regression_from_json_bad_payload_still_errors(configured_app_with_task: Path, tmp_path):
    runner.invoke(app, ["spec", "new", "--title", "Decision queue", "--id", "dq"])
    jf = tmp_path / "bad.json"; jf.write_text("this is not json at all")
    result = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf)])
    assert result.exit_code != 0
    # A valid-JSON but schema-invalid payload still errors via the JSON path
    # (never routed through the markdown parser).
    jf2 = tmp_path / "partial.json"; jf2.write_text('{"problem": "only problem"}')
    result2 = runner.invoke(app, ["spec", "apply", "dq", "--from-json", str(jf2)])
    assert result2.exit_code != 0


def test_spec_dispatch_closes_links_issue_to_work_item(configured_app_with_task: Path, tmp_path: Path):
    """--closes on dispatch links the issue ref to the task's WorkItem (#386)."""
    _approve_add_labels(configured_app_with_task, tmp_path)
    result = runner.invoke(app, ["spec", "dispatch", "add-labels", "--closes", "acme/widgets#9"])
    assert result.exit_code == 0, result.output
    mgr = StateManager(configured_app_with_task / ".mothership")
    wi_id = mgr.load().tasks["add-labels"].work_item_id
    assert wi_id
    from mship.core.workitem_store import WorkItemStore
    item = WorkItemStore(configured_app_with_task / ".mothership" / "workitems").get(wi_id)
    assert [l.title for l in item.external_links] == ["acme/widgets#9"]


def test_spec_dispatch_closes_invalid_ref_fails_loud(configured_app_with_task: Path, tmp_path: Path):
    _approve_add_labels(configured_app_with_task, tmp_path)
    result = runner.invoke(app, ["spec", "dispatch", "add-labels", "--closes", "nope"])
    assert result.exit_code == 1


def test_spec_review_human_mode_renders_prose_verdicts(configured_app_with_task: Path, tmp_path):
    """The chat-approval recipe points at `spec review` to find flagged prose —
    the human (TTY) rendering must actually show section id + verdict."""
    from mship.cli.output import reset_output_settings
    _apply_dq(tmp_path)
    assert runner.invoke(app, ["spec", "verdict", "dq", "approach", "flagged"]).exit_code == 0
    reset_output_settings()
    result = runner.invoke(app, ["spec", "review", "dq"], env={"MSHIP_JSON": "0"})
    assert result.exit_code == 0, result.output
    assert "prose approach" in result.output   # section id, labeled as prose
    assert "[flagged] prose approach" in result.output  # verdict rendered literally
