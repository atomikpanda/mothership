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
