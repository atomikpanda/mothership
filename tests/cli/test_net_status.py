import json

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.cli.output import reset_output_settings
from mship.core.topology import Edge, Topology

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_settings():
    """`configure_output` writes process-global state, so a `--json` invocation
    here would otherwise force JSON mode for every later test in the run (the
    same pattern as tests/cli/test_output_flags.py)."""
    reset_output_settings()
    yield
    reset_output_settings()


@pytest.fixture(autouse=True)
def _in_a_workspace(workspace, monkeypatch):
    """`mship net status` resolves its workspace config from the CURRENT
    DIRECTORY, so without this the tests only pass when pytest happens to run
    from inside an mship workspace — they failed in a bare clone of this repo,
    where no `mothership.yaml` is discoverable, with a bare `SystemExit(1)`.

    Same `workspace` + `chdir` pattern as tests/cli/test_doctor.py.
    """
    monkeypatch.chdir(workspace)


def _fake_topology():
    return Topology(
        version=1, workspace="ws", probed_at="2026-07-25T16:00:00+00:00",
        edges=[
            Edge(kind="serve", name="serve", status="ok", code="serve_relay_running",
                 detail="relay-serve pid 1 running", fix=None, facts={"mode": "relay"}),
            Edge(kind="run_host", name="run_host:mac", status="fail",
                 code="run_host_unmapped", detail="no connection mapped",
                 fix="run `mship run-host add mac`", facts={}),
        ],
    )


def test_json_mode_emits_the_payload(monkeypatch):
    monkeypatch.setattr("mship.core.topology.probe_topology", lambda **kw: _fake_topology())
    result = runner.invoke(app, ["net", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["version"] == 1
    assert payload["edges"][1]["code"] == "run_host_unmapped"


def test_human_mode_shows_status_and_fix(monkeypatch):
    monkeypatch.setattr("mship.core.topology.probe_topology", lambda **kw: _fake_topology())
    # MSHIP_JSON=0 forces human output on a pipe (CliRunner is never a TTY).
    monkeypatch.setenv("MSHIP_JSON", "0")
    result = runner.invoke(app, ["net", "status"])
    assert result.exit_code == 0
    # Assert on rendering only human mode produces — the edge name and the fix
    # text also appear in the JSON body, so they alone would pass vacuously.
    assert "Connectivity" in result.stdout          # the table title
    assert "Next steps:" in result.stdout           # the fix section
    assert "run-host add mac" in result.stdout


def test_global_json_flag_forces_the_payload(monkeypatch):
    monkeypatch.setattr("mship.core.topology.probe_topology", lambda **kw: _fake_topology())
    monkeypatch.setenv("MSHIP_JSON", "0")           # human by env...
    result = runner.invoke(app, ["--json", "net", "status"])   # ...overridden by the flag
    assert result.exit_code == 0
    assert json.loads(result.stdout)["version"] == 1


def test_exits_zero_even_when_everything_is_broken(monkeypatch):
    """AC4: this command must work precisely when connectivity is broken."""
    broken = Topology(version=1, workspace="ws", probed_at="t", edges=[
        Edge(kind="relay", name="relay", status="fail", code="relay_unreachable",
             detail="down", fix="restart serve", facts={}),
    ])
    monkeypatch.setattr("mship.core.topology.probe_topology", lambda **kw: broken)
    assert runner.invoke(app, ["net", "status"]).exit_code == 0


def test_no_network_flag_is_passed_through(monkeypatch):
    seen = {}

    def spy(**kw):
        seen.update(kw)
        return _fake_topology()

    monkeypatch.setattr("mship.core.topology.probe_topology", spy)
    assert runner.invoke(app, ["net", "status", "--no-network"]).exit_code == 0
    assert seen["skip_network"] is True
