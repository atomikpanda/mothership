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
