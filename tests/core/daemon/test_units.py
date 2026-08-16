"""Rendered units are PARSED, not substring-matched: systemd directives are
section-scoped and a directive in the wrong section is silently ignored (only
an "Unknown lvalue" journal line), so flat `in` assertions would pass CI while
the backoff policy is broken on the host."""
import configparser
import plistlib
import sys
from pathlib import Path

import pytest

from mship.core.daemon.units import (
    LAUNCHD_LABEL,
    DaemonExecResolutionError,
    render_launchd_plist,
    render_systemd_unit,
    resolve_mshipd_argv,
)

ARGV = ["/opt/venv/bin/mshipd"]


def _parse_unit(text: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read_string(text)
    return cp


def test_systemd_unit_sections():
    cp = _parse_unit(render_systemd_unit(ARGV))
    assert cp["Service"]["ExecStart"] == "/opt/venv/bin/mshipd"
    assert cp["Service"]["Restart"] == "on-failure"
    assert cp["Service"]["RestartSec"] == "5"
    assert cp["Unit"]["StartLimitIntervalSec"] == "300"
    assert cp["Unit"]["StartLimitBurst"] == "5"
    assert cp["Install"]["WantedBy"] == "default.target"
    # Loser exits 0 (run.py), so no SuccessExitStatus mapping exists — and none
    # exists for launchd anyway.
    assert "SuccessExitStatus" not in cp["Service"]


def test_systemd_unit_has_no_working_directory():
    """The workspace-agnostic assumption, made enforceable: a WorkingDirectory
    would bake one workspace into the one-per-host daemon."""
    text = render_systemd_unit(ARGV)
    assert "WorkingDirectory" not in text


def test_systemd_multiarg_exec_is_joined():
    cp = _parse_unit(render_systemd_unit(["/usr/bin/python3", "-m", "mship.core.daemon"]))
    assert cp["Service"]["ExecStart"] == "/usr/bin/python3 -m mship.core.daemon"


def test_launchd_plist_shape(tmp_path: Path):
    log_dir = tmp_path / "logs"
    plist = plistlib.loads(render_launchd_plist(ARGV, log_dir).encode())
    assert plist["Label"] == LAUNCHD_LABEL == "com.mothership.daemon"
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ThrottleInterval"] == 5
    assert plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == ARGV
    # launchd discards stderr otherwise — the last-resort net for output that
    # escapes the logging tree.
    assert plist["StandardOutPath"] == str(log_dir / "launchd.out.log")
    assert plist["StandardErrorPath"] == str(log_dir / "launchd.err.log")


def test_execstart_resolves_sibling_first(tmp_path, monkeypatch):
    """Same venv bin dir ⇒ provably same dist (the uv-tool-install layout);
    sibling wins even when PATH has a different mshipd."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "python"
    exe.touch()
    sibling = bin_dir / "mshipd"
    sibling.touch()
    monkeypatch.setattr(sys, "executable", str(exe))
    argv = resolve_mshipd_argv(which=lambda name: "/somewhere/else/mshipd")
    assert argv == [str(sibling)]


def test_which_fallback_verified_against_sys_prefix(tmp_path, monkeypatch):
    prefix = tmp_path / "venv"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "python"
    exe.touch()
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "prefix", str(prefix))

    inside = bin_dir / "shims" / "mshipd"
    inside.parent.mkdir()
    inside.touch()
    assert resolve_mshipd_argv(which=lambda name: str(inside)) == [str(inside)]

    # A PATH shim from ANOTHER install (stale uv-tool shim while running
    # `uv run mship` from a checkout) must refuse, not silently bake a
    # worktree venv into a persistent unit.
    outside = tmp_path / "other" / "mshipd"
    outside.parent.mkdir()
    outside.touch()
    with pytest.raises(DaemonExecResolutionError, match="dev tree"):
        resolve_mshipd_argv(which=lambda name: str(outside))


def test_module_fallback_when_nothing_resolvable(tmp_path, monkeypatch):
    prefix = tmp_path / "venv"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "python"
    exe.touch()
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "prefix", str(prefix))
    argv = resolve_mshipd_argv(which=lambda name: None)
    assert argv == [str(exe), "-m", "mship.core.daemon"]
