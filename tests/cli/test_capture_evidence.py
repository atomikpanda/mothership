"""`mship capture --evidence <spec>:<ac>` — promoting a capture into
acceptance-criterion evidence in ONE command.

The manual "capture, then attach" second step in practice never happens, so the
behaviour worth pinning is: with the flag the artifact lands in the durable
store AND on the criterion; without it nothing is stored (bare capture must not
even create the store); and a storage failure warns without turning a good
capture into a bad exit code.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.spec import AcceptanceCriterion, Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()

_NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeShell:
    """Stands in for util/shell.py::ShellRunner.

    `run_task` plays the repo's `capture` target (writes
    `$MSHIP_CAPTURE_DIR/screen.png`); `run` plays the two git commands
    `provenance_note` shells out for — note its command is a STRING, not argv.
    """

    def __init__(self, *, dirty: bool = False):
        self._dirty = dirty
        self.commands: list[str] = []

    def run_task(self, task_name, actual_task_name, cwd, env_runner=None, env=None):
        out = Path(env["MSHIP_CAPTURE_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "screen.png").write_bytes(b"PNGDATA")
        return _FakeResult()

    def run(self, command, cwd=None, **kwargs):
        assert isinstance(command, str), "ShellRunner.run takes a command string"
        self.commands.append(command)
        if command.startswith("git rev-parse"):
            return _FakeResult(stdout="abc1234\n")
        if command.startswith("git status"):
            return _FakeResult(stdout=" M src/app.kt\n" if self._dirty else "")
        if command.startswith("git branch"):
            return _FakeResult(stdout="* main\n")  # on a real branch, by default
        return _FakeResult()


@pytest.fixture
def workspace_with_spec(tmp_path: Path):
    """Workspace with one capture-able repo, an active task, and spec `dq`
    carrying criterion `ac1` (modelled on test_log.py::configured_app_with_task)."""
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "Taskfile.yml").write_text(
        "version: '3'\n"
        "tasks:\n"
        "  capture:\n"
        "    cmds:\n"
        "      - touch $MSHIP_CAPTURE_DIR/screen.png\n"
    )
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "    capture:\n"
        "      platforms: [android]\n"
    )
    StateManager(state_dir).save(WorkspaceState(tasks={
        "t": Task(
            slug="t", description="d", phase="dev", created_at=_NOW,
            affected_repos=["app"], worktrees={"app": str(wt)},
            branch="feat/t", base_branch="main", active_repo="app",
        )
    }))
    SpecStore(tmp_path / "specs").save(Spec(
        id="dq", title="Dequeue", status="approved",
        created_at=_NOW, updated_at=_NOW, affected_repos=["app"],
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="The card clears.")],
        body="## Problem\n\nCards linger.\n",
    ))

    shell = _FakeShell()
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    container.shell.override(shell)
    try:
        yield tmp_path
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
        container.state_manager.reset_override()
        container.state_manager.reset()
        container.shell.reset_override()


def _criterion(workspace: Path):
    spec = SpecStore(workspace / "specs").find_by_id("dq")
    assert spec is not None
    return spec.acceptance_criteria[0]


def test_evidence_attaches_to_the_named_criterion(workspace_with_spec: Path):
    result = runner.invoke(
        app, ["capture", "--task", "t", "--repo", "app", "--evidence", "dq:ac1"]
    )
    assert result.exit_code == 0, result.output
    # --evidence must not add a line to STDOUT: that stream is the capture
    # payload, and an agent runs `mship capture --evidence ... | jq`.
    assert json.loads(result.stdout)["artifacts"][0]["kind"] == "image"

    crit = _criterion(workspace_with_spec)
    assert len(crit.evidence) == 1
    ev = crit.evidence[0]
    assert ev.kind == "artifact"
    assert ev.ref.endswith(".png")
    # The provenance marker: a reviewer can see WHICH tree the shot came from.
    assert "at " in (ev.note or "")
    # The bytes actually landed in the durable, spec-scoped store.
    stored = workspace_with_spec / ".mothership" / "evidence" / "dq" / ev.ref
    assert stored.read_bytes() == b"PNGDATA"


def test_bare_capture_attaches_nothing(workspace_with_spec: Path):
    result = runner.invoke(app, ["capture", "--task", "t", "--repo", "app"])
    assert result.exit_code == 0, result.output

    assert _criterion(workspace_with_spec).evidence == []
    # Not merely empty — bare capture must not create the store at all.
    assert not (workspace_with_spec / ".mothership" / "evidence").exists()


def test_store_failure_warns_but_capture_succeeds(workspace_with_spec: Path, monkeypatch):
    def _boom(*a, **kw):
        raise OSError("disk went away")

    # Patched where `_attach_evidence` LOOKS the name up (it imports inside the
    # function body), not where the caller re-exports it.
    monkeypatch.setattr("mship.core.evidence_store.store_artifact", _boom)

    result = runner.invoke(
        app, ["capture", "--task", "t", "--repo", "app", "--evidence", "dq:ac1"]
    )
    assert result.exit_code == 0, result.output
    assert "could not attach evidence" in result.output
    assert "disk went away" in result.output
    assert "Traceback" not in (result.output or "")
    assert _criterion(workspace_with_spec).evidence == []


def test_malformed_evidence_target_is_actionable(workspace_with_spec: Path):
    """A typo'd --evidence must say what the shape is, not fail obscurely."""
    result = runner.invoke(
        app, ["capture", "--task", "t", "--repo", "app", "--evidence", "dq"]
    )
    assert result.exit_code == 0, result.output
    assert "could not attach evidence" in result.output
    assert "<spec-id>:<criterion-id>" in result.output
    assert _criterion(workspace_with_spec).evidence == []

def test_capture_evidence_does_not_restore_stale_spec_after_apply(
    workspace_with_spec: Path, monkeypatch,
):
    """A capture that starts before apply must not save its pre-apply snapshot."""
    from mship.core.spec_draft import SpecDraft, apply_draft_transaction

    apply_started = Event()
    capture_locked = Event()
    apply_result: dict[str, object] = {}
    original_locked = SpecStore.locked

    @contextmanager
    def record_capture_lock(self, spec_id):
        if current_thread().name == "MainThread":
            capture_locked.set()
        with original_locked(self, spec_id) as artifact:
            yield artifact

    def apply_replacement() -> None:
        apply_started.set()
        try:
            apply_draft_transaction(
                SpecStore(workspace_with_spec / "specs"),
                "dq",
                SpecDraft(
                    problem="New problem",
                    user_story="New story",
                    approach="New approach",
                    acceptance_criteria=["replacement criterion"],
                ),
                bypass_status_gate=True,
                discard_review=True,
            )
        except BaseException as exc:
            apply_result["error"] = exc

    def start_apply(*_args, **_kwargs) -> str:
        thread = Thread(target=apply_replacement)
        apply_result["thread"] = thread
        thread.start()
        assert apply_started.wait(timeout=5)
        # Before the lock fix this blocks until apply has persisted the
        # replacement, forcing the stale capture save to run afterward. With
        # the fix capture already holds the lock, so let it release it instead.
        if not capture_locked.is_set():
            thread.join(timeout=5)
            assert not thread.is_alive()
        return "capture.png"

    monkeypatch.setattr(SpecStore, "locked", record_capture_lock)
    monkeypatch.setattr("mship.core.evidence_store.store_artifact", start_apply)
    result = runner.invoke(
        app, ["capture", "--task", "t", "--repo", "app", "--evidence", "dq:ac1"]
    )
    assert result.exit_code == 0, result.output
    assert capture_locked.is_set()
    thread = apply_result["thread"]
    assert isinstance(thread, Thread)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert "error" not in apply_result

    persisted = SpecStore(workspace_with_spec / "specs").find_by_id("dq")
    assert persisted is not None
    assert persisted.acceptance_criteria[0].text == "replacement criterion"
    assert "New approach" in persisted.body
