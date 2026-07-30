import json
from pathlib import Path
import typer
from typer.testing import CliRunner

def _app(tmp_path):
    from mship.cli.plan import register
    class FakeContainer:
        def config_path(self): return str(tmp_path / "mothership.yaml")
    app = typer.Typer(); register(app, lambda: FakeContainer()); return app

def test_check_assumptions_reports_missing(tmp_path):
    plans = tmp_path / "docs" / "plans"; plans.mkdir(parents=True)
    (plans / "2026-07-29-x.md").write_text(
        "## Assumptions checked\n- repo topology — meta\n"
    )
    runner = CliRunner()
    res = runner.invoke(_app(tmp_path), ["plan", "check-assumptions", "--plan", str(plans / "2026-07-29-x.md")])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["ok"] is False
    assert "credential locus" in data["missing"]
    assert "repo topology" not in data["missing"]

def test_check_assumptions_ok_when_all_covered(tmp_path):
    from mship.core.plan import SEED_AXES
    plans = tmp_path / "docs" / "plans"; plans.mkdir(parents=True)
    body = "## Assumptions checked\n" + "".join(f"- {a} — ok\n" for a in SEED_AXES)
    (plans / "2026-07-29-y.md").write_text(body)
    runner = CliRunner()
    res = runner.invoke(_app(tmp_path), ["plan", "check-assumptions", "--plan", str(plans / "2026-07-29-y.md")])
    data = json.loads(res.output)
    assert data["ok"] is True and data["missing"] == []

def test_check_assumptions_reads_expected_from_store_when_present(tmp_path):
    from mship.core.assumptions import AssumptionRow, AssumptionStore
    from mship.core.plan import SEED_AXES

    store = AssumptionStore(tmp_path)
    rows = store.seed()
    rows.append(AssumptionRow(axis="new axis", options="a / b", position="a", triggers="new"))
    store.save(rows)

    plans = tmp_path / "docs" / "plans"; plans.mkdir(parents=True)
    body = "## Assumptions checked\n" + "".join(f"- {a} — ok\n" for a in SEED_AXES)
    (plans / "2026-07-29-z.md").write_text(body)

    runner = CliRunner()
    res = runner.invoke(_app(tmp_path), ["plan", "check-assumptions", "--plan", str(plans / "2026-07-29-z.md")])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["ok"] is False
    assert "new axis" in data["missing"]
    assert "new axis" in data["expected"]

def test_check_assumptions_falls_back_to_seed_axes_with_no_store(tmp_path):
    from mship.core.plan import SEED_AXES

    plans = tmp_path / "docs" / "plans"; plans.mkdir(parents=True)
    body = "## Assumptions checked\n" + "".join(f"- {a} — ok\n" for a in SEED_AXES)
    (plans / "2026-07-29-w.md").write_text(body)

    assert not (tmp_path / "docs" / "product_assumptions.md").exists()

    runner = CliRunner()
    res = runner.invoke(_app(tmp_path), ["plan", "check-assumptions", "--plan", str(plans / "2026-07-29-w.md")])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["ok"] is True and data["missing"] == []
    assert data["expected"] == list(SEED_AXES)
