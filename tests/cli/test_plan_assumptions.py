import json
from datetime import datetime, timezone
from pathlib import Path

import typer
from typer.testing import CliRunner

from mship.core.state import StateManager, Task, WorkspaceState


def _task(slug="t1", **kw) -> Task:
    defaults = dict(
        slug=slug,
        description="Add retry logic for flaky pushes.",
        phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"],
        branch="feat/x",
    )
    defaults.update(kw)
    return Task(**defaults)


def _app(tmp_path, task: Task | None = None):
    from mship.cli.plan import register

    class FakeContainer:
        def config_path(self):
            return str(tmp_path / "mothership.yaml")

        def state_dir(self):
            return tmp_path / ".mothership"

        def state_manager(self):
            return StateManager(tmp_path / ".mothership")

    container = FakeContainer()
    if task is not None:
        container.state_manager().save(WorkspaceState(tasks={task.slug: task}))

    app = typer.Typer()
    register(app, lambda: container)
    return app


def _write_plan(tmp_path: Path, name: str, body: str) -> Path:
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / name
    path.write_text(body)
    return path


def test_check_emit_contains_axes_plan_text_and_no_journal_or_trace_markers(tmp_path):
    from mship.core.assumptions import AssumptionStore, SEED_ROWS
    from mship.core.log import LogManager

    AssumptionStore(tmp_path).seed()

    plan_body = "# My Plan\n\nSome unique plan sentence XYZZY-PLAN-MARKER.\n"
    plan_path = _write_plan(tmp_path, "2026-07-30-t1.md", plan_body)

    # A real journal entry with a unique marker: the prompt must not surface
    # journal/trace/codegraph content, only the request/rows/plan inputs.
    mgr = LogManager(tmp_path / ".mothership" / "logs")
    mgr.create("t1")
    mgr.append("t1", "JOURNAL-CANARY-998877 codegraph reasoning trace", action="note")

    runner = CliRunner()
    res = runner.invoke(
        _app(tmp_path, task=_task()),
        ["plan", "assumptions", "check", "--task", "t1", "--plan", str(plan_path), "--emit"],
    )
    assert res.exit_code == 0, res.output

    out = res.output
    for row in SEED_ROWS:
        assert row.axis in out
    assert "XYZZY-PLAN-MARKER" in out
    assert "JOURNAL-CANARY-998877" not in out


def test_check_without_emit_errors(tmp_path):
    from mship.core.assumptions import AssumptionStore

    AssumptionStore(tmp_path).seed()
    plan_path = _write_plan(tmp_path, "2026-07-30-t1.md", "# Plan\n")

    runner = CliRunner()
    res = runner.invoke(
        _app(tmp_path, task=_task()),
        ["plan", "assumptions", "check", "--task", "t1", "--plan", str(plan_path)],
    )
    assert res.exit_code != 0


def test_result_not_covered_stores_pending_checker_flag(tmp_path):
    from mship.core.plan_check import PlanCheckStore

    AssumptionStore = __import__("mship.core.assumptions", fromlist=["AssumptionStore"]).AssumptionStore
    AssumptionStore(tmp_path).seed()

    plan_body = "# Plan\n\nNo mention of anything special here.\n"
    plan_path = _write_plan(tmp_path, "2026-07-30-t1.md", plan_body)

    verdicts_file = tmp_path / "verdicts.json"
    verdicts_file.write_text(json.dumps([
        {"axis": "repo topology", "verdict": "not-covered", "reason": "plan never says how repos are handled"},
    ]))

    runner = CliRunner()
    res = runner.invoke(
        _app(tmp_path, task=_task()),
        [
            "plan", "assumptions", "result",
            "--task", "t1", "--plan", str(plan_path),
            "--from-json", str(verdicts_file),
        ],
    )
    assert res.exit_code == 0, res.output

    stored = PlanCheckStore(tmp_path / ".mothership").get("t1")
    assert stored is not None
    checker_flags = [f for f in stored.flags if f.source == "checker" and f.axis == "repo topology"]
    assert len(checker_flags) == 1
    assert checker_flags[0].approved is False

    data = json.loads(res.output)
    assert data["task"] == "t1"
    assert data["pending"] >= 1


def test_result_na_verdict_on_triggered_axis_stores_cross_check_flag(tmp_path):
    from mship.core.assumptions import AssumptionStore, SEED_ROWS
    from mship.core.plan_check import PlanCheckStore

    AssumptionStore(tmp_path).seed()

    # "auth" is a trigger word for the "credential locus" axis; putting it in
    # the plan text should force a disposition for that axis.
    plan_body = "# Plan\n\nThis plan adds auth handling to the login flow.\n"
    plan_path = _write_plan(tmp_path, "2026-07-30-t1.md", plan_body)

    # Cover every other axis so only the credential-locus n/a produces a flag.
    verdicts = [
        {"axis": row.axis, "verdict": "covered", "reason": "n/a for this test"}
        for row in SEED_ROWS
        if row.axis != "credential locus"
    ]
    verdicts.append({"axis": "credential locus", "verdict": "n-a", "reason": "declared not applicable"})

    verdicts_file = tmp_path / "verdicts.json"
    verdicts_file.write_text(json.dumps(verdicts))

    runner = CliRunner()
    res = runner.invoke(
        _app(tmp_path, task=_task()),
        [
            "plan", "assumptions", "result",
            "--task", "t1", "--plan", str(plan_path),
            "--from-json", str(verdicts_file),
        ],
    )
    assert res.exit_code == 0, res.output

    stored = PlanCheckStore(tmp_path / ".mothership").get("t1")
    assert stored is not None
    cross_flags = [f for f in stored.flags if f.source == "cross-check" and f.axis == "credential locus"]
    assert len(cross_flags) == 1


def test_result_stored_plan_hash_matches_plan_text(tmp_path):
    from mship.core.assumptions import AssumptionStore
    from mship.core.plan_check import PlanCheckStore, plan_hash

    AssumptionStore(tmp_path).seed()

    plan_body = "# Plan\n\nHash-check body.\n"
    plan_path = _write_plan(tmp_path, "2026-07-30-t1.md", plan_body)

    verdicts_file = tmp_path / "verdicts.json"
    verdicts_file.write_text(json.dumps([
        {"axis": "repo topology", "verdict": "covered", "reason": "ok"},
    ]))

    runner = CliRunner()
    res = runner.invoke(
        _app(tmp_path, task=_task()),
        [
            "plan", "assumptions", "result",
            "--task", "t1", "--plan", str(plan_path),
            "--from-json", str(verdicts_file),
        ],
    )
    assert res.exit_code == 0, res.output

    stored = PlanCheckStore(tmp_path / ".mothership").get("t1")
    assert stored is not None
    assert stored.plan_hash == plan_hash(plan_path.read_text())


def test_status_reports_fresh_false_when_plan_changed_after_check(tmp_path):
    AssumptionStore = __import__("mship.core.assumptions", fromlist=["AssumptionStore"]).AssumptionStore
    AssumptionStore(tmp_path).seed()

    plan_path = _write_plan(tmp_path, "2026-07-30-t1.md", "# Plan\n\nOriginal body.\n")

    verdicts_file = tmp_path / "verdicts.json"
    verdicts_file.write_text(json.dumps([
        {"axis": "repo topology", "verdict": "covered", "reason": "ok"},
    ]))

    runner = CliRunner()
    app = _app(tmp_path, task=_task())
    res = runner.invoke(
        app,
        [
            "plan", "assumptions", "result",
            "--task", "t1", "--plan", str(plan_path),
            "--from-json", str(verdicts_file),
        ],
    )
    assert res.exit_code == 0, res.output

    # Plan changes after the check was stored -> hash mismatch -> stale.
    plan_path.write_text("# Plan\n\nOriginal body, now edited.\n")

    res = runner.invoke(
        app,
        ["plan", "assumptions", "status", "--task", "t1", "--plan", str(plan_path)],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["task"] == "t1"
    assert data["fresh"] is False


def test_approve_clears_exactly_one_pending_flag_and_drops_pending_count(tmp_path):
    from mship.core.plan_check import PlanCheckStore

    AssumptionStore = __import__("mship.core.assumptions", fromlist=["AssumptionStore"]).AssumptionStore
    AssumptionStore(tmp_path).seed()

    plan_path = _write_plan(tmp_path, "2026-07-30-t1.md", "# Plan\n\nNo mention of anything special here.\n")

    verdicts_file = tmp_path / "verdicts.json"
    verdicts_file.write_text(json.dumps([
        {"axis": "repo topology", "verdict": "not-covered", "reason": "plan never says how repos are handled"},
    ]))

    runner = CliRunner()
    app = _app(tmp_path, task=_task())
    res = runner.invoke(
        app,
        [
            "plan", "assumptions", "result",
            "--task", "t1", "--plan", str(plan_path),
            "--from-json", str(verdicts_file),
        ],
    )
    assert res.exit_code == 0, res.output

    before = PlanCheckStore(tmp_path / ".mothership").get("t1")
    pending_before = sum(1 for f in before.flags if not f.approved)
    assert pending_before == 1

    res = runner.invoke(
        app,
        [
            "plan", "assumptions", "approve", "repo topology",
            "--reason", "signed off by hand",
            "--task", "t1", "--plan", str(plan_path),
        ],
    )
    assert res.exit_code == 0, res.output

    after = PlanCheckStore(tmp_path / ".mothership").get("t1")
    pending_after = sum(1 for f in after.flags if not f.approved)
    assert pending_after == pending_before - 1

    matching = [f for f in after.flags if f.axis == "repo topology"]
    assert len(matching) == 1
    assert matching[0].approved is True
    assert matching[0].approved_reason == "signed off by hand"
    assert matching[0].approved_by


def test_approve_unknown_or_already_covered_axis_exits_1(tmp_path):
    AssumptionStore = __import__("mship.core.assumptions", fromlist=["AssumptionStore"]).AssumptionStore
    AssumptionStore(tmp_path).seed()

    plan_path = _write_plan(tmp_path, "2026-07-30-t1.md", "# Plan\n\nNo mention of anything special here.\n")

    verdicts_file = tmp_path / "verdicts.json"
    verdicts_file.write_text(json.dumps([
        {"axis": "repo topology", "verdict": "covered", "reason": "handled explicitly"},
    ]))

    runner = CliRunner()
    app = _app(tmp_path, task=_task())
    res = runner.invoke(
        app,
        [
            "plan", "assumptions", "result",
            "--task", "t1", "--plan", str(plan_path),
            "--from-json", str(verdicts_file),
        ],
    )
    assert res.exit_code == 0, res.output

    # Axis has a verdict but no pending flag (it was covered, not not-covered).
    res = runner.invoke(
        app,
        ["plan", "assumptions", "approve", "repo topology", "--task", "t1", "--plan", str(plan_path)],
    )
    assert res.exit_code == 1

    # Axis that never existed at all.
    res = runner.invoke(
        app,
        ["plan", "assumptions", "approve", "nonexistent axis", "--task", "t1", "--plan", str(plan_path)],
    )
    assert res.exit_code == 1


def test_result_preserves_prior_approvals_when_plan_unchanged(tmp_path):
    """Re-running the checker against the SAME plan (unchanged hash) must NOT
    wipe a human sign-off; a CHANGED plan correctly drops it (Wave 3a review)."""
    from mship.core.plan_check import PlanCheckStore

    AssumptionStore = __import__("mship.core.assumptions", fromlist=["AssumptionStore"]).AssumptionStore
    AssumptionStore(tmp_path).seed()

    plan_path = _write_plan(tmp_path, "2026-07-30-t1.md", "# Plan\n\nOriginal body.\n")

    verdicts_file = tmp_path / "verdicts.json"
    verdicts_file.write_text(json.dumps([
        {"axis": "repo topology", "verdict": "not-covered", "reason": "plan never says how repos are handled"},
    ]))

    runner = CliRunner()
    app = _app(tmp_path, task=_task())

    def _result():
        return runner.invoke(app, [
            "plan", "assumptions", "result",
            "--task", "t1", "--plan", str(plan_path), "--from-json", str(verdicts_file),
        ])

    assert _result().exit_code == 0
    res = runner.invoke(app, [
        "plan", "assumptions", "approve", "repo topology",
        "--reason", "signed off", "--task", "t1", "--plan", str(plan_path),
    ])
    assert res.exit_code == 0, res.output

    # Re-check the SAME plan text -> the sign-off survives.
    assert _result().exit_code == 0
    stored = PlanCheckStore(tmp_path / ".mothership").get("t1")
    match = [f for f in stored.flags if f.axis == "repo topology"]
    assert len(match) == 1 and match[0].approved is True
    assert match[0].approved_reason == "signed off"

    # Re-running the cold checker on the SAME plan may RE-WORD the same gap. The
    # sign-off keys on (axis, source), not the LLM's free-text reason, so a
    # re-phrased reason must NOT drop a real approval (Wave 3a re-review).
    verdicts_file.write_text(json.dumps([
        {"axis": "repo topology", "verdict": "not-covered", "reason": "totally different wording for the same gap"},
    ]))
    assert _result().exit_code == 0
    stored = PlanCheckStore(tmp_path / ".mothership").get("t1")
    match = [f for f in stored.flags if f.axis == "repo topology"]
    assert len(match) == 1 and match[0].approved is True

    # Edit the plan -> hash changes -> the stale approval is dropped.
    plan_path.write_text("# Plan\n\nOriginal body, now edited.\n")
    assert _result().exit_code == 0
    stored = PlanCheckStore(tmp_path / ".mothership").get("t1")
    match = [f for f in stored.flags if f.axis == "repo topology"]
    assert len(match) == 1 and match[0].approved is False


def test_result_at_linked_nonconvention_plan_lets_gate_pass(tmp_path):
    """End-to-end: a feature WorkItem whose plan is linked at a NON-convention
    path. `result` (no --plan) must record the check against the SAME plan the
    gate reads (the WorkItem's linked plan_path), so a fresh, flag-free check
    makes the assumption gate pass. Before the shared `effective_plan_path`, the
    CLI hashed the convention path while the gate hashed the linked path, so the
    hashes never matched and the WorkItem was permanently mis-gated."""
    from mship.core.assumptions import AssumptionStore, SEED_ROWS
    from mship.core.spec import Spec
    from mship.core.spec_store import SpecStore
    from mship.core.workitem_gate import check_task_gate
    from mship.core.workitem_store import WorkItemStore

    AssumptionStore(tmp_path).seed()

    # Feature WorkItem + approved spec so only the assumption gate is in play.
    now = datetime.now(timezone.utc)
    specs = SpecStore(tmp_path / "specs")
    specs.save(Spec(id="spec-1", title="Spec", status="approved", created_at=now, updated_at=now))
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=now)
    items.link_spec(wi.id, "spec-1", now=now)

    # Plan lives OFF the convention path; link it explicitly.
    plan_body = "# Plan\n\n<!-- mship:task id=1 -->\n### Task 1\n<!-- /mship:task -->\n"
    custom = tmp_path / "custom" / "myplan.md"
    custom.parent.mkdir(parents=True)
    custom.write_text(plan_body)
    items.link_plan(wi.id, "custom/myplan.md", now=now)

    task = _task(work_item_id=wi.id)

    # Every axis covered, no trigger words -> zero flags.
    verdicts_file = tmp_path / "verdicts.json"
    verdicts_file.write_text(json.dumps(
        [{"axis": row.axis, "verdict": "covered", "reason": "ok"} for row in SEED_ROWS]
    ))

    runner = CliRunner()
    app = _app(tmp_path, task=task)
    # No --plan: effective_plan_path must follow the WorkItem's linked plan_path.
    res = runner.invoke(app, [
        "plan", "assumptions", "result", "--task", "t1", "--from-json", str(verdicts_file),
    ])
    assert res.exit_code == 0, res.output

    gate = check_task_gate(task, tmp_path, require_plan=True, require_assumption_gate=True)
    assert gate.ok is True, gate.reason
