"""`mship finish` refuses to open a PR when the task has no WorkItem (or a
feature WorkItem without an approved spec). `--hotfix` downgrades the block to
a warning and records a bypass-log entry.

See core/workitem_gate.py::check_task_gate (task 1/6) and spec
workitem-mandatory-kind-gated-approval, task 4/6.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.spec import Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager
from mship.core.workitem_store import WorkItemStore
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def finish_gate_workspace(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    def _default_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        if "rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    mock_shell.run.side_effect = _default_run
    container.shell.override(mock_shell)

    yield workspace_with_git, mock_shell
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_finish_blocks_when_task_has_no_work_item(finish_gate_workspace):
    """A task spawned with --hotfix has work_item_id=None; finish must refuse."""
    workspace, _ = finish_gate_workspace
    result = runner.invoke(app, ["spawn", "--hotfix", "no workitem task", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--task", "no-workitem-task"])
    assert result.exit_code == 1, result.output
    assert "no WorkItem" in result.output

    # No PR should have been opened.
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["no-workitem-task"].pr_urls == {}


def test_finish_blocks_feature_work_item_without_approved_spec(finish_gate_workspace):
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    wi = items.create(title="add thing", kind="feature", workspace="ws",
                       now=datetime.now(timezone.utc))

    result = runner.invoke(
        app, ["spawn", "--work-item", wi.id, "feature task", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--task", "feature-task"])
    assert result.exit_code == 1, result.output
    assert "approved spec" in result.output

    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["feature-task"].pr_urls == {}


def test_finish_passes_with_bug_work_item_and_no_spec(finish_gate_workspace):
    """bug/chore/question WorkItems satisfy the gate without any spec at all."""
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    wi = items.create(title="fix it", kind="bug", workspace="ws",
                       now=datetime.now(timezone.utc))

    result = runner.invoke(
        app, ["spawn", "--work-item", wi.id, "bug task", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--task", "bug-task"])
    assert result.exit_code == 0, result.output

    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["bug-task"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"


_PLAN_DOC = "# Plan\n\n<!-- mship:task id=1 -->\n### Task 1\n<!-- /mship:task -->\n"


def _write_plan(workspace: Path, slug: str) -> None:
    d = workspace / "docs" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"2026-07-12-{slug}.md").write_text(_PLAN_DOC)


def test_finish_passes_with_feature_work_item_and_approved_spec(finish_gate_workspace):
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    specs = SpecStore(workspace / "specs")
    now = datetime.now(timezone.utc)
    specs.save(Spec(id="spec-1", title="Spec", status="approved",
                    created_at=now, updated_at=now))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=now)
    items.link_spec(wi.id, "spec-1", now=now)

    result = runner.invoke(
        app, ["spawn", "--work-item", wi.id, "feature with spec", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output
    # finish now also requires a plan (require_plan=True) — write one.
    _write_plan(workspace, "feature-with-spec")

    result = runner.invoke(app, ["finish", "--task", "feature-with-spec"])
    assert result.exit_code == 0, result.output

    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["feature-with-spec"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"


def test_finish_blocks_feature_work_item_without_plan(finish_gate_workspace):
    """Feature with an approved spec but no implementation plan → finish refuses."""
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    specs = SpecStore(workspace / "specs")
    now = datetime.now(timezone.utc)
    specs.save(Spec(id="spec-1", title="Spec", status="approved",
                    created_at=now, updated_at=now))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=now)
    items.link_spec(wi.id, "spec-1", now=now)

    result = runner.invoke(
        app, ["spawn", "--work-item", wi.id, "feature no plan", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--task", "feature-no-plan"])
    assert result.exit_code == 1, result.output
    assert "plan" in result.output.lower()

    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["feature-no-plan"].pr_urls == {}


def test_finish_hotfix_bypasses_plan_gate(finish_gate_workspace):
    """--hotfix downgrades the plan-gate block to a warning and still opens the PR."""
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    specs = SpecStore(workspace / "specs")
    now = datetime.now(timezone.utc)
    specs.save(Spec(id="spec-1", title="Spec", status="approved",
                    created_at=now, updated_at=now))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=now)
    items.link_spec(wi.id, "spec-1", now=now)

    result = runner.invoke(
        app, ["spawn", "--work-item", wi.id, "feature hotfix plan", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output

    # No plan on disk → --hotfix rescues the finish.
    result = runner.invoke(app, ["finish", "--hotfix", "--task", "feature-hotfix-plan"])
    assert result.exit_code == 0, result.output
    assert "WorkItem gate bypassed" in result.output

    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["feature-hotfix-plan"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"


def test_finish_hotfix_warns_and_proceeds_and_logs_bypass(finish_gate_workspace):
    workspace, _ = finish_gate_workspace
    result = runner.invoke(app, ["spawn", "--hotfix", "hotfix finish task", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--hotfix", "--task", "hotfix-finish-task"])
    assert result.exit_code == 0, result.output
    assert "WorkItem gate bypassed" in result.output
    assert "--hotfix" in result.output

    # PR still gets opened — hotfix downgrades the block to a warning.
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["hotfix-finish-task"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"

    # Bypass is recorded to the shared bypass log.
    bypass_log = workspace / ".mothership" / "bypass-log.jsonl"
    assert bypass_log.is_file()
    lines = [json.loads(line) for line in bypass_log.read_text().splitlines()]
    assert any(
        entry["op"] == "finish" and entry["branch"] == "hotfix-finish-task" and entry["reason"] == "hotfix"
        for entry in lines
    ), lines


# ---------------------------------------------------------------------------
# PR review fix: corrupt/unreadable WorkItem store must not raise a raw
# exception out of `finish` — that would skip the --hotfix rescue entirely.
# ---------------------------------------------------------------------------

def test_finish_blocks_cleanly_on_corrupt_workitem_store(finish_gate_workspace):
    """Without --hotfix: a corrupt store degrades to a clean, actionable
    `output.error` + exit 1 — never a traceback."""
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    wi = items.create(title="add thing", kind="bug", workspace="ws",
                       now=datetime.now(timezone.utc))

    result = runner.invoke(
        app, ["spawn", "--work-item", wi.id, "corrupt store task", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output

    # Corrupt the WorkItem's JSON file on disk (spawn already validated it
    # while it was still well-formed).
    wi_path = workspace / ".mothership" / "workitems" / f"{wi.id}.json"
    wi_path.write_text("{not valid json")

    result = runner.invoke(app, ["finish", "--task", "corrupt-store-task"])
    assert result.exit_code == 1, result.output
    assert "Cannot finish" in result.output
    assert "Traceback" not in result.output

    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["corrupt-store-task"].pr_urls == {}


def test_finish_hotfix_survives_corrupt_workitem_store(finish_gate_workspace):
    """With --hotfix: the gate-evaluation exception is caught and downgraded
    to a failing GateResult, so the pre-existing --hotfix → warn + proceed
    path still rescues the task."""
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    wi = items.create(title="add thing", kind="bug", workspace="ws",
                       now=datetime.now(timezone.utc))

    result = runner.invoke(
        app, ["spawn", "--work-item", wi.id, "corrupt store hotfix task", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output

    wi_path = workspace / ".mothership" / "workitems" / f"{wi.id}.json"
    wi_path.write_text("{not valid json")

    result = runner.invoke(app, ["finish", "--hotfix", "--task", "corrupt-store-hotfix-task"])
    assert result.exit_code == 0, result.output
    assert "WorkItem gate bypassed" in result.output
    assert "corrupt store" in result.output.lower()

    # PR still gets opened — hotfix downgrades the block to a warning.
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["corrupt-store-hotfix-task"].pr_urls.get("shared") == \
        "https://github.com/org/shared/pull/1"

    bypass_log = workspace / ".mothership" / "bypass-log.jsonl"
    assert bypass_log.is_file()
    lines = [json.loads(line) for line in bypass_log.read_text().splitlines()]
    assert any(
        entry["op"] == "finish" and entry["branch"] == "corrupt-store-hotfix-task"
        and entry["reason"] == "hotfix"
        for entry in lines
    ), lines


# ---------------------------------------------------------------------------
# Acceptance-criteria evidence gate (ac-evidence-loop): WARN by default when
# any AC on the task's bound spec lacks evidence; BLOCK only under
# --require-evidence. No bound spec ⇒ no-op.
# ---------------------------------------------------------------------------

def _seed_feature_with_ac(workspace: Path, *, ac_evidence=None):
    """Feature WI + approved spec whose ac1 has (or lacks) evidence, linked so
    resolve_bound_spec finds it via wi.spec_id."""
    from mship.core.spec import AcceptanceCriterion, Spec
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    specs = SpecStore(workspace / "specs")
    now = datetime.now(timezone.utc)
    specs.save(Spec(id="ev-spec", title="S", status="approved", created_at=now, updated_at=now,
                    acceptance_criteria=[AcceptanceCriterion(
                        id="ac1", text="x", verdict="approved", evidence=ac_evidence or [])]))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=now)
    items.link_spec(wi.id, "ev-spec", now=now)
    return wi


def test_finish_warns_by_default_on_acs_without_evidence(finish_gate_workspace):
    workspace, _ = finish_gate_workspace
    wi = _seed_feature_with_ac(workspace)   # ac1 has NO evidence
    runner.invoke(app, ["spawn", "--work-item", wi.id, "ev warn", "--repos", "shared"])
    _write_plan(workspace, "ev-warn")       # plan-gate satisfied
    result = runner.invoke(app, ["finish", "--task", "ev-warn"])
    assert result.exit_code == 0, result.output          # WARN only → still finishes
    # Assert on output UNIQUE to the AC-evidence gate (the pre-existing test-evidence
    # gate also prints "evidence", so a bare "evidence" check would pass even if the
    # AC gate never fired). The bound spec id + the --require-evidence hint are only
    # emitted by the AC gate.
    out = result.output.lower()
    assert "ev-spec" in out and "ac1" in out             # names the bound spec + the unverified AC
    assert "--require-evidence" in result.output         # the AC gate's escalation hint
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["ev-warn"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"


def test_finish_blocks_under_require_evidence(finish_gate_workspace):
    workspace, _ = finish_gate_workspace
    wi = _seed_feature_with_ac(workspace)   # ac1 has NO evidence
    runner.invoke(app, ["spawn", "--work-item", wi.id, "ev block", "--repos", "shared"])
    _write_plan(workspace, "ev-block")
    result = runner.invoke(app, ["finish", "--task", "ev-block", "--require-evidence"])
    assert result.exit_code == 1, result.output
    # Blocked specifically by the AC-evidence gate (names the bound spec + AC), not by
    # some unrelated failure — exit 1 alone could come from anywhere.
    assert "ev-spec" in result.output.lower() and "ac1" in result.output.lower()
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["ev-block"].pr_urls == {}          # blocked before any PR


def test_finish_require_evidence_noop_without_bound_spec(finish_gate_workspace):
    """No bound spec (a bug WI) ⇒ --require-evidence is a no-op, not a block."""
    workspace, _ = finish_gate_workspace
    items = WorkItemStore(workspace / ".mothership" / "workitems")
    wi = items.create(title="fix it", kind="bug", workspace="ws", now=datetime.now(timezone.utc))
    runner.invoke(app, ["spawn", "--work-item", wi.id, "ev noop", "--repos", "shared"])
    result = runner.invoke(app, ["finish", "--task", "ev-noop", "--require-evidence"])
    assert result.exit_code == 0, result.output
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["ev-noop"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"


def test_finish_appends_acceptance_block_to_pr_body(finish_gate_workspace):
    from mship.core.spec import AcceptanceEvidence
    workspace, mock_shell = finish_gate_workspace
    wi = _seed_feature_with_ac(
        workspace, ac_evidence=[AcceptanceEvidence(kind="test", ref="test-runs/1")],
    )
    runner.invoke(app, ["spawn", "--work-item", wi.id, "ev body", "--repos", "shared"])
    _write_plan(workspace, "ev-body")
    result = runner.invoke(app, ["finish", "--task", "ev-body"])
    assert result.exit_code == 0, result.output
    create_cmds = [c.args[0] for c in mock_shell.run.call_args_list if "gh pr create" in c.args[0]]
    assert create_cmds, "expected a gh pr create call"
    assert "Acceptance criteria" in create_cmds[0]      # block injected into the PR body
    assert "test:test-runs/1" in create_cmds[0]         # the evidence ref renders end-to-end
    assert "ac1" in create_cmds[0]                        # the criterion id is listed
