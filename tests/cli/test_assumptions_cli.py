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


def test_cli_honors_non_default_docs_dir(tmp_path):
    """`mship assumptions` must write to <docs_dir>/ so its edits are visible to
    check-assumptions and the plan-phase injection, which honor docs_dir
    (final-review #2)."""
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos: {}\ndocs_dir: customdocs\n"
    )
    res = CliRunner().invoke(_app(tmp_path), ["assumptions", "list"])  # auto-seeds
    assert res.exit_code == 0, res.output
    assert (tmp_path / "customdocs" / "product_assumptions.md").is_file()
    assert not (tmp_path / "docs" / "product_assumptions.md").exists()


def test_add_on_fresh_workspace_seeds_baseline_first(tmp_path):
    """`add` as the FIRST assumptions command must seed the 7 baseline rows, not
    save only the added one and drop the baseline (Greptile #450)."""
    from mship.core.assumptions import SEED_ROWS
    res = CliRunner().invoke(_app(tmp_path), [
        "assumptions", "add", "--axis", "new axis", "--options", "a/b",
        "--position", "p", "--triggers", "t",
    ])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["count"] == len(SEED_ROWS) + 1  # 7 seed + 1, not 1


def test_list_on_malformed_store_errors_cleanly(tmp_path):
    """A malformed store surfaces a clean CLI error, not a traceback (Greptile #450)."""
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "product_assumptions.md").write_text(
        "| axis | options | position | triggers |\n| -- | -- | -- | -- |\n| a | b | c |\n"
    )
    res = CliRunner().invoke(_app(tmp_path), ["assumptions", "list"])
    assert res.exit_code == 1
    assert "malformed" in res.output
