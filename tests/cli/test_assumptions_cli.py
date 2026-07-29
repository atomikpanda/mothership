import json

import typer
from typer.testing import CliRunner


def _app(tmp_path):
    from mship.cli.assumptions import register

    class FakeContainer:
        def config_path(self):
            return str(tmp_path / "mothership.yaml")

    app = typer.Typer()
    register(app, lambda: FakeContainer())
    return app


def test_list_auto_seeds_fresh_workspace(tmp_path):
    runner = CliRunner()
    res = runner.invoke(_app(tmp_path), ["assumptions", "list"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["count"] == 7
    assert len(data["rows"]) == 7
    assert data["rows"][0]["axis"] == "repo topology"
    assert (tmp_path / "docs" / "product_assumptions.md").is_file()


def test_add_appends_row(tmp_path):
    runner = CliRunner()
    app = _app(tmp_path)
    runner.invoke(app, ["assumptions", "list"])  # seed first
    res = runner.invoke(
        app,
        [
            "assumptions",
            "add",
            "--axis",
            "new axis",
            "--options",
            "a / b",
            "--position",
            "**a**",
            "--triggers",
            "foo, bar",
        ],
    )
    assert res.exit_code == 0, res.output

    from mship.core.assumptions import AssumptionStore

    store = AssumptionStore(tmp_path, docs_dir="docs")
    rows = store.load()
    assert len(rows) == 8
    assert any(r.axis == "new axis" and r.position == "**a**" for r in rows)


def test_edit_changes_position(tmp_path):
    runner = CliRunner()
    app = _app(tmp_path)
    runner.invoke(app, ["assumptions", "list"])  # seed first
    res = runner.invoke(
        app,
        ["assumptions", "edit", "repo topology", "--position", "**changed**"],
    )
    assert res.exit_code == 0, res.output

    from mship.core.assumptions import AssumptionStore

    store = AssumptionStore(tmp_path, docs_dir="docs")
    rows = store.load()
    row = next(r for r in rows if r.axis == "repo topology")
    assert row.position == "**changed**"


def test_render_prints_all_axes(tmp_path):
    runner = CliRunner()
    app = _app(tmp_path)
    runner.invoke(app, ["assumptions", "list"])  # seed first
    res = runner.invoke(app, ["assumptions", "render"])
    assert res.exit_code == 0, res.output
    from mship.core.assumptions import SEED_ROWS

    for row in SEED_ROWS:
        assert row.axis in res.output


def test_encrypted_mode_writes_enc_file(tmp_path):
    (tmp_path / "mothership.yaml").write_text(
        "workspace: test\nassumption_storage: encrypted\n"
    )
    runner = CliRunner()
    res = runner.invoke(_app(tmp_path), ["assumptions", "list"])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "docs" / "product_assumptions.md.enc").is_file()
    assert not (tmp_path / "docs" / "product_assumptions.md").exists()
